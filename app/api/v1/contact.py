"""Public Contact Us API (landing page inquiries) — minimal latency path."""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.contact_schema import ContactInquiryCreate
from app.services.contact_service import create_contact_inquiry

router = APIRouter()
logger = logging.getLogger(__name__)

# Light in-process rate limit (per worker). Enough for contact spam; no Redis.
_RATE_WINDOW_S = 600
_RATE_LIMIT = 8
_hits: dict[str, list[float]] = defaultdict(list)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:64] or "unknown"
    if request.client and request.client.host:
        return request.client.host[:64]
    return "unknown"


def _is_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    window_start = now - _RATE_WINDOW_S
    stamps = [t for t in _hits[ip] if t >= window_start]
    if len(stamps) >= _RATE_LIMIT:
        _hits[ip] = stamps
        return True
    stamps.append(now)
    _hits[ip] = stamps
    return False


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Submit a landing-page contact inquiry",
)
def submit_contact_inquiry(
    payload: ContactInquiryCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Persist a Contact Us / study inquiry from the public landing page.

    Fast path: one INSERT, no SELECT/refresh, tiny JSON body.
    Email notification can be added later via email_notified_at.
    """
    ip = _client_ip(request)
    if _is_rate_limited(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many inquiries. Please try again in a few minutes.",
        )

    # Honeypot — accept without writing
    if payload.website:
        return JSONResponse({"ok": True}, status_code=status.HTTP_201_CREATED)

    try:
        create_contact_inquiry(db, payload, client_ip=ip)
        return JSONResponse({"ok": True}, status_code=status.HTTP_201_CREATED)
    except Exception:
        logger.exception("Failed to save contact inquiry")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not save your inquiry. Please try again.",
        ) from None
