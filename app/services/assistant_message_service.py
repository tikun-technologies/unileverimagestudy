"""Fast, private assistant chat persistence with reverse keyset pagination."""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional, Tuple
from uuid import UUID

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.assistant_message_model import AssistantConversation, AssistantMessage
from app.schemas.assistant_schema import (
    AssistantFollowUpContext,
    AssistantHistoryItem,
    AssistantHistoryMeta,
    AssistantHistoryPage,
    AssistantQueryResponse,
)

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 50


class AssistantMessageServiceError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def encode_cursor(created_at: datetime, message_id: UUID) -> str:
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    payload = {
        "c": created_at.astimezone(timezone.utc).isoformat(),
        "i": str(message_id),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: Optional[str]) -> Optional[Tuple[datetime, UUID]]:
    if not cursor:
        return None
    try:
        padded = cursor + ("=" * (-len(cursor) % 4))
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        created_at = datetime.fromisoformat(str(payload["c"]))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        message_id = UUID(str(payload["i"]))
        return created_at, message_id
    except Exception as exc:
        raise AssistantMessageServiceError("Invalid pagination cursor", status_code=400) from exc


def get_or_create_conversation(
    db: Session,
    *,
    study_id: UUID,
    user_id: UUID,
) -> AssistantConversation:
    existing = db.scalar(
        select(AssistantConversation).where(
            AssistantConversation.study_id == study_id,
            AssistantConversation.user_id == user_id,
        )
    )
    if existing:
        return existing

    conversation = AssistantConversation(study_id=study_id, user_id=user_id)
    db.add(conversation)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(AssistantConversation).where(
                AssistantConversation.study_id == study_id,
                AssistantConversation.user_id == user_id,
            )
        )
        if existing:
            return existing
        raise
    db.refresh(conversation)
    return conversation


def get_conversation(
    db: Session,
    *,
    study_id: UUID,
    user_id: UUID,
) -> Optional[AssistantConversation]:
    return db.scalar(
        select(AssistantConversation).where(
            AssistantConversation.study_id == study_id,
            AssistantConversation.user_id == user_id,
        )
    )


def insert_user_message(
    db: Session,
    *,
    conversation: AssistantConversation,
    content: str,
    client_message_id: str,
) -> AssistantMessage:
    """
    Idempotently insert a user message.

    Retries with the same client_message_id return the existing row.
    """
    client_message_id = (client_message_id or "").strip()[:64]
    if not client_message_id:
        raise AssistantMessageServiceError("client_message_id is required", status_code=400)

    existing = db.scalar(
        select(AssistantMessage).where(
            AssistantMessage.conversation_id == conversation.id,
            AssistantMessage.client_message_id == client_message_id,
        )
    )
    if existing:
        return existing

    message = AssistantMessage(
        conversation_id=conversation.id,
        study_id=conversation.study_id,
        user_id=conversation.user_id,
        role="user",
        content=content,
        client_message_id=client_message_id,
        status="complete",
    )
    db.add(message)
    conversation.updated_at = _utc_now()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(AssistantMessage).where(
                AssistantMessage.conversation_id == conversation.id,
                AssistantMessage.client_message_id == client_message_id,
            )
        )
        if existing:
            return existing
        raise
    db.refresh(message)
    return message


def get_assistant_reply_for_parent(
    db: Session,
    *,
    parent_message_id: UUID,
) -> Optional[AssistantMessage]:
    return db.scalar(
        select(AssistantMessage).where(
            AssistantMessage.parent_message_id == parent_message_id,
            AssistantMessage.role == "assistant",
        )
    )


def insert_assistant_message(
    db: Session,
    *,
    conversation: AssistantConversation,
    parent_message: AssistantMessage,
    content: str,
    response: AssistantQueryResponse,
    status: str = "complete",
) -> AssistantMessage:
    """
    Persist an assistant reply linked to its parent user message.

    Idempotent on parent_message_id — retries return the existing reply.
    """
    existing = get_assistant_reply_for_parent(db, parent_message_id=parent_message.id)
    if existing:
        return existing

    payload = json.loads(response.model_dump_json())
    message = AssistantMessage(
        conversation_id=conversation.id,
        study_id=conversation.study_id,
        user_id=conversation.user_id,
        role="assistant",
        content=content,
        parent_message_id=parent_message.id,
        response_payload=payload,
        status=status if status in {"pending", "complete", "error", "failed"} else "complete",
    )
    db.add(message)

    follow_up = response.follow_up_context
    if follow_up is not None:
        conversation.follow_up_context = json.loads(follow_up.model_dump_json())
    conversation.updated_at = _utc_now()

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = get_assistant_reply_for_parent(db, parent_message_id=parent_message.id)
        if existing:
            return existing
        raise
    db.refresh(message)
    return message


def update_conversation_follow_up(
    db: Session,
    *,
    conversation: AssistantConversation,
    follow_up: Optional[AssistantFollowUpContext],
) -> None:
    if follow_up is None:
        return
    conversation.follow_up_context = json.loads(follow_up.model_dump_json())
    conversation.updated_at = _utc_now()
    db.commit()


def _message_to_history_item(message: AssistantMessage) -> AssistantHistoryItem:
    response = None
    if message.role == "assistant" and isinstance(message.response_payload, dict):
        try:
            response = AssistantQueryResponse(**message.response_payload)
            response.assistant_message_id = message.id
            response.user_message_id = message.parent_message_id
            response.conversation_id = message.conversation_id
        except Exception:
            response = None
    return AssistantHistoryItem(
        id=message.id,
        role=message.role,  # type: ignore[arg-type]
        content=message.content,
        created_at=message.created_at,
        client_message_id=message.client_message_id,
        parent_message_id=message.parent_message_id,
        status=message.status,
        response=response,
    )


def list_messages_page(
    db: Session,
    *,
    study_id: UUID,
    user_id: UUID,
    limit: int = DEFAULT_HISTORY_LIMIT,
    before: Optional[str] = None,
) -> AssistantHistoryPage:
    """
    Reverse keyset page: newest N messages (or older than `before`).

    Returns items oldest→newest for direct UI append/prepend.
    Never counts all rows and never uses OFFSET.
    """
    limit = max(1, min(int(limit or DEFAULT_HISTORY_LIMIT), MAX_HISTORY_LIMIT))
    conversation = get_conversation(db, study_id=study_id, user_id=user_id)
    if not conversation:
        return AssistantHistoryPage(
            items=[],
            meta=AssistantHistoryMeta(
                limit=limit,
                has_more=False,
                next_cursor=None,
                conversation_id=None,
            ),
            follow_up_context=None,
        )

    cursor = decode_cursor(before)
    stmt = (
        select(AssistantMessage)
        .where(AssistantMessage.conversation_id == conversation.id)
        .order_by(AssistantMessage.created_at.desc(), AssistantMessage.id.desc())
        .limit(limit + 1)
    )
    if cursor:
        cursor_ts, cursor_id = cursor
        stmt = stmt.where(
            or_(
                AssistantMessage.created_at < cursor_ts,
                and_(
                    AssistantMessage.created_at == cursor_ts,
                    AssistantMessage.id < cursor_id,
                ),
            )
        )

    rows = list(db.scalars(stmt).all())
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    # Reverse to chronological order for the UI.
    page_rows.reverse()

    next_cursor = None
    if has_more and page_rows:
        oldest = page_rows[0]
        next_cursor = encode_cursor(oldest.created_at, oldest.id)

    follow_up = None
    if isinstance(conversation.follow_up_context, dict):
        try:
            follow_up = AssistantFollowUpContext(**conversation.follow_up_context)
        except Exception:
            follow_up = None

    return AssistantHistoryPage(
        items=[_message_to_history_item(m) for m in page_rows],
        meta=AssistantHistoryMeta(
            limit=limit,
            has_more=has_more,
            next_cursor=next_cursor,
            conversation_id=conversation.id,
        ),
        follow_up_context=follow_up,
    )


def clear_conversation_messages(
    db: Session,
    *,
    study_id: UUID,
    user_id: UUID,
) -> int:
    """Delete only the current user's messages for this study. Returns deleted count."""
    conversation = get_conversation(db, study_id=study_id, user_id=user_id)
    if not conversation:
        return 0

    result = db.execute(
        delete(AssistantMessage).where(AssistantMessage.conversation_id == conversation.id)
    )
    conversation.follow_up_context = None
    conversation.updated_at = _utc_now()
    db.commit()
    return int(result.rowcount or 0)


def response_from_assistant_message(
    message: AssistantMessage,
    *,
    fallback_request_id: Optional[str] = None,
) -> Optional[AssistantQueryResponse]:
    if not isinstance(message.response_payload, dict):
        return None
    try:
        response = AssistantQueryResponse(**message.response_payload)
        if fallback_request_id:
            response.request_id = fallback_request_id
        response.user_message_id = message.parent_message_id
        response.assistant_message_id = message.id
        return response
    except Exception:
        return None
