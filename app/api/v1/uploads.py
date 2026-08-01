from __future__ import annotations

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, status
import asyncio
from typing import List
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_active_user
from app.db.session import get_db
from app.models.user_model import User
from app.services.cloudinary_service import upload_file, upload_base64, delete_public_id

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


@router.delete("")
def delete_asset(
    public_id: str,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    ok = delete_public_id(public_id)
    return {"success": ok}



