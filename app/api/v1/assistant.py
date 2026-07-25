"""Verified analytics assistant API routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_active_user
from app.db.session import get_db
from app.models.user_model import User
from app.schemas.assistant_schema import AssistantQueryRequest, AssistantQueryResponse
from app.services.assistant_service import run_assistant_query
from app.services.assistant_tools import AssistantToolError

router = APIRouter()


@router.post("/{study_id}/assistant/query", response_model=AssistantQueryResponse)
def assistant_query(
    study_id: UUID,
    payload: AssistantQueryRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Ask a verified analytics/design question about a study.

    GPT-4o-mini only builds a tiny query plan. All facts are computed
    deterministically from analysis tools (low token cost, no hallucinations).
    """
    try:
        return run_assistant_query(db, study_id, current_user, payload)
    except AssistantToolError as exc:
        raise HTTPException(status_code=400 if exc.status != "error" else 400, detail=exc.message)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Assistant query failed: {exc}") from exc
