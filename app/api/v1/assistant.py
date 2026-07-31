"""Verified analytics assistant API routes."""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_active_user
from app.db.session import get_db
from app.models.user_model import User
from app.schemas.assistant_schema import (
    AssistantClearHistoryResponse,
    AssistantHistoryPage,
    AssistantQueryRequest,
    AssistantQueryResponse,
)
from app.services.assistant_message_service import (
    AssistantMessageServiceError,
    DEFAULT_HISTORY_LIMIT,
    MAX_HISTORY_LIMIT,
    clear_conversation_messages,
    get_conversation,
    list_messages_page,
)
from app.services.assistant_service import run_assistant_query
from app.services.assistant_tools import AssistantToolError, authorize_study_for_assistant

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
    User and assistant turns are persisted privately per (study, user).
    """
    try:
        return run_assistant_query(db, study_id, current_user, payload)
    except AssistantToolError as exc:
        raise HTTPException(status_code=400 if exc.status != "error" else 400, detail=exc.message)
    except AssistantMessageServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Assistant query failed: {exc}") from exc


@router.get("/{study_id}/assistant/messages", response_model=AssistantHistoryPage)
def assistant_messages(
    study_id: UUID,
    limit: int = Query(DEFAULT_HISTORY_LIMIT, ge=1, le=MAX_HISTORY_LIMIT),
    before: Optional[str] = Query(
        None,
        description="Opaque keyset cursor for older messages (from meta.next_cursor).",
        max_length=512,
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Load the latest page of this user's private assistant chat for the study.

    Pass `before` (next_cursor) to fetch the next older page. Never returns the
    full history in one call.
    """
    try:
        authorize_study_for_assistant(db, study_id, current_user)
        return list_messages_page(
            db,
            study_id=study_id,
            user_id=current_user.id,
            limit=limit,
            before=before,
        )
    except AssistantToolError as exc:
        raise HTTPException(status_code=403 if "denied" in (exc.message or "").lower() else 400, detail=exc.message)
    except AssistantMessageServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load assistant history: {exc}") from exc


@router.delete("/{study_id}/assistant/messages", response_model=AssistantClearHistoryResponse)
def clear_assistant_messages(
    study_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Clear only the current user's assistant chat for this study."""
    try:
        authorize_study_for_assistant(db, study_id, current_user)
        conversation = get_conversation(db, study_id=study_id, user_id=current_user.id)
        conversation_id = conversation.id if conversation else None
        deleted = clear_conversation_messages(
            db,
            study_id=study_id,
            user_id=current_user.id,
        )
        return AssistantClearHistoryResponse(
            deleted=deleted,
            conversation_id=conversation_id,
        )
    except AssistantToolError as exc:
        raise HTTPException(status_code=403 if "denied" in (exc.message or "").lower() else 400, detail=exc.message)
    except AssistantMessageServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to clear assistant history: {exc}") from exc
