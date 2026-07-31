"""Durable per-user, per-study analytics assistant chat history."""

from __future__ import annotations

import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base


class AssistantConversation(Base):
    """
    One private conversation timeline per (study_id, user_id).

    Study members share study access but never share chat history.
    """

    __tablename__ = "assistant_conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    study_id = Column(
        UUID(as_uuid=True),
        ForeignKey("studies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Bounded follow-up planner state restored after refresh.
    follow_up_context = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    study = relationship("Study", back_populates="assistant_conversations", lazy="noload")
    user = relationship("User", back_populates="assistant_conversations", lazy="noload")
    messages = relationship(
        "AssistantMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
        lazy="noload",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("study_id", "user_id", name="uq_assistant_conversations_study_user"),
        Index("idx_assistant_conversations_study_user", "study_id", "user_id"),
        Index("idx_assistant_conversations_user_updated", "user_id", "updated_at"),
    )


class AssistantMessage(Base):
    """
    Append-only chat message for a private assistant conversation.

    Keyset pagination uses (created_at DESC, id DESC).
    """

    __tablename__ = "assistant_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    study_id = Column(
        UUID(as_uuid=True),
        ForeignKey("studies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    # Client-stable UUID for optimistic UI + idempotent retries (user messages).
    client_message_id = Column(String(64), nullable=True)
    # Links assistant reply to its parent user message.
    parent_message_id = Column(
        UUID(as_uuid=True),
        ForeignKey("assistant_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Full AssistantQueryResponse JSON for faithful UI restore.
    response_payload = Column(JSONB, nullable=True)
    status = Column(String(30), nullable=False, server_default="complete")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    conversation = relationship("AssistantConversation", back_populates="messages", lazy="noload")
    parent_message = relationship(
        "AssistantMessage",
        remote_side=[id],
        lazy="noload",
        uselist=False,
    )

    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="ck_assistant_messages_role"),
        CheckConstraint(
            "status IN ('pending', 'complete', 'error', 'failed')",
            name="ck_assistant_messages_status",
        ),
        # Idempotent user inserts / retries.
        UniqueConstraint(
            "conversation_id",
            "client_message_id",
            name="uq_assistant_messages_conversation_client_id",
        ),
        # One assistant answer per user parent (retry-safe).
        UniqueConstraint(
            "parent_message_id",
            name="uq_assistant_messages_parent_message_id",
        ),
        # Hot path: reverse keyset pagination within a conversation.
        Index(
            "idx_assistant_messages_conversation_created_id",
            "conversation_id",
            "created_at",
            "id",
        ),
        Index("idx_assistant_messages_study_user_created", "study_id", "user_id", "created_at"),
    )
