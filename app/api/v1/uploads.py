from __future__ import annotations

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, status
import asyncio
import logging
from typing import List
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import get_current_active_user
from app.db.session import SessionLocal, get_db
from app.models.user_model import User
from app.services.cloudinary_service import (
    upload_file,
    upload_base64,
    upload_video_file,
    delete_public_id,
    generate_video_upload_sas,
    _public_url,
)
from app.services.video_encode_service import (
    enqueue_encode,
    register_uploaded_video,
    summarize_video_urls,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/image")
def upload_image(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    try:
        secure_url, public_id = upload_file(file)
        return {"secure_url": secure_url, "public_id": public_id}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Upload failed")


@router.post("/image-base64")
def upload_image_base64(
    payload: dict,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    data_url = payload.get("data_url")
    if not data_url:
        raise HTTPException(status_code=400, detail="data_url is required")
    try:
        secure_url, public_id = upload_base64(data_url)
        return {"secure_url": secure_url, "public_id": public_id}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Upload failed")


@router.post("/images")
async def upload_images(
    files: List[UploadFile] = File(...),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    # Bounded concurrency to improve stability and overall throughput under load
    max_concurrency = 8
    sem = asyncio.Semaphore(max_concurrency)

    async def _upload_one(idx: int, f: UploadFile):
        async with sem:
            try:
                secure_url, public_id = await asyncio.to_thread(upload_file, f)
                return {"index": idx, "ok": True, "secure_url": secure_url, "public_id": public_id}
            except Exception as exc:  # ValueError -> validation; others -> generic failure
                msg = str(exc) if isinstance(exc, ValueError) else "Upload failed"
                return {"index": idx, "ok": False, "error": msg}

    tasks = [asyncio.create_task(_upload_one(i, f)) for i, f in enumerate(files or [])]
    completed = await asyncio.gather(*tasks)

    results = [
        {"secure_url": it["secure_url"], "public_id": it["public_id"], "index": it["index"]}
        for it in completed if it.get("ok")
    ]
    errors = [
        {"index": it["index"], "error": it.get("error", "Upload failed")}
        for it in completed if not it.get("ok")
    ]

    return {"results": results, "errors": errors}


@router.post("/videos")
async def upload_videos(
    files: List[UploadFile] = File(...),
    current_user: User = Depends(get_current_active_user),
):
    max_concurrency = 3
    sem = asyncio.Semaphore(max_concurrency)

    async def _upload_one(idx: int, f: UploadFile):
        async with sem:
            try:
                secure_url, public_id = await asyncio.to_thread(upload_video_file, f)
                return {"index": idx, "ok": True, "secure_url": secure_url, "public_id": public_id}
            except Exception as exc:
                msg = str(exc) if isinstance(exc, ValueError) else "Upload failed"
                return {"index": idx, "ok": False, "error": msg}

    tasks = [asyncio.create_task(_upload_one(i, f)) for i, f in enumerate(files or [])]
    completed = await asyncio.gather(*tasks)
    results = []
    errors = [
        {"index": it["index"], "error": it.get("error", "Upload failed")}
        for it in completed if not it.get("ok")
    ]
    queued = []
    record_db = SessionLocal()
    try:
        for it in completed:
            if not it.get("ok"):
                continue
            try:
                asset = register_uploaded_video(record_db, it["public_id"], it["secure_url"], str(current_user.id))
                queued.append((it, asset))
            except Exception:
                logger.exception("Could not record video %s", it.get("public_id"))
                record_db.rollback()
                results.append({
                    "secure_url": it["secure_url"],
                    "public_id": it["public_id"],
                    "index": it["index"],
                    "status": "failed",
                })

        async def _enqueue(item: dict, asset):
            try:
                await asyncio.to_thread(enqueue_encode, asset, str(current_user.id))
                return item, asset.status
            except Exception as exc:
                logger.exception("Video encode was not queued for %s", asset.public_id)
                asset.status = "failed"
                asset.error = str(exc)
                return item, "failed"

        started = await asyncio.gather(*[_enqueue(item, asset) for item, asset in queued])
        if any(asset.status == "failed" for _, asset in queued):
            record_db.commit()
        for item, encode_status in started:
            results.append({
                "secure_url": item["secure_url"],
                "public_id": item["public_id"],
                "index": item["index"],
                "status": encode_status,
            })
    finally:
        record_db.close()
    results.sort(key=lambda row: row["index"])
    return {"results": results, "errors": errors}


@router.post("/videos/sas")
def create_video_upload_sas(
    payload: dict,
    current_user: User = Depends(get_current_active_user),
):
    """Issue a SAS so the browser can upload a video directly to Blob storage.

    Removes the API-as-relay hop, which is the main cause of slow video uploads.
    """
    filename = (payload or {}).get("filename") or "upload.mp4"
    try:
        return generate_video_upload_sas(filename)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        logger.exception("Could not create video upload SAS")
        raise HTTPException(status_code=500, detail="Could not start upload")


@router.post("/videos/complete")
def complete_video_upload(
    payload: dict,
    current_user: User = Depends(get_current_active_user),
):
    """Register a directly-uploaded video and enqueue its HLS encode."""
    public_id = (payload or {}).get("public_id")
    if not public_id or not isinstance(public_id, str) or not public_id.startswith("videos/"):
        raise HTTPException(status_code=400, detail="A valid public_id is required")

    container = settings.AZURE_STORAGE_CONTAINER or "videos"
    source_url = _public_url(container, public_id)
    db = SessionLocal()
    try:
        asset = register_uploaded_video(db, public_id, source_url, str(current_user.id))
        try:
            enqueue_encode(asset, str(current_user.id))
            encode_status = asset.status
        except Exception as exc:
            logger.exception("Video encode was not queued for %s", public_id)
            asset.status = "failed"
            asset.error = str(exc)
            db.commit()
            encode_status = "failed"
        return {
            "result": {
                # Store the source MP4 URL so playback works during encoding;
                # the encode job swaps it for the HLS URL when ready.
                "secure_url": source_url,
                "public_id": public_id,
                "index": 0,
                "status": encode_status,
            }
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Could not complete video upload %s", public_id)
        db.rollback()
        raise HTTPException(status_code=500, detail="Could not finalize upload")
    finally:
        db.close()


@router.post("/videos/status")
def video_encode_status(
    payload: dict,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Whether uploaded videos have finished HLS encoding, plus the playback URL to use."""
    urls = (payload or {}).get("urls")
    if urls is None:
        urls = []
    if not isinstance(urls, list):
        raise HTTPException(status_code=400, detail="urls must be a list")
    return summarize_video_urls(db, urls)


@router.delete("")
def delete_asset(
    public_id: str,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    ok = delete_public_id(public_id)
    return {"success": ok}



