"""add assistant conversations and messages

Revision ID: 20260729_assistant_chat
Revises: 20260713_layer_templates
Create Date: 2026-07-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260729_assistant_chat"
down_revision: Union[str, Sequence[str], None] = "20260713_layer_templates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assistant_conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("study_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("follow_up_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["study_id"], ["studies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("study_id", "user_id", name="uq_assistant_conversations_study_user"),
    )
    op.create_index("ix_assistant_conversations_study_id", "assistant_conversations", ["study_id"], unique=False)
    op.create_index("ix_assistant_conversations_user_id", "assistant_conversations", ["user_id"], unique=False)
    op.create_index(
        "idx_assistant_conversations_study_user",
        "assistant_conversations",
        ["study_id", "user_id"],
        unique=False,
    )
    op.create_index(
        "idx_assistant_conversations_user_updated",
        "assistant_conversations",
        ["user_id", "updated_at"],
        unique=False,
    )

    op.create_table(
        "assistant_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("study_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("client_message_id", sa.String(length=64), nullable=True),
        sa.Column("parent_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="complete"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["assistant_conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["study_id"], ["studies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_message_id"], ["assistant_messages.id"], ondelete="SET NULL"),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_assistant_messages_role"),
        sa.CheckConstraint(
            "status IN ('pending', 'complete', 'error', 'failed')",
            name="ck_assistant_messages_status",
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "client_message_id",
            name="uq_assistant_messages_conversation_client_id",
        ),
        sa.UniqueConstraint("parent_message_id", name="uq_assistant_messages_parent_message_id"),
    )
    op.create_index("ix_assistant_messages_conversation_id", "assistant_messages", ["conversation_id"], unique=False)
    op.create_index("ix_assistant_messages_study_id", "assistant_messages", ["study_id"], unique=False)
    op.create_index("ix_assistant_messages_user_id", "assistant_messages", ["user_id"], unique=False)
    op.create_index("ix_assistant_messages_parent_message_id", "assistant_messages", ["parent_message_id"], unique=False)
    op.create_index(
        "idx_assistant_messages_conversation_created_id",
        "assistant_messages",
        ["conversation_id", "created_at", "id"],
        unique=False,
        postgresql_ops={"created_at": "DESC", "id": "DESC"},
    )
    op.create_index(
        "idx_assistant_messages_study_user_created",
        "assistant_messages",
        ["study_id", "user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_assistant_messages_study_user_created", table_name="assistant_messages")
    op.drop_index("idx_assistant_messages_conversation_created_id", table_name="assistant_messages")
    op.drop_index("ix_assistant_messages_parent_message_id", table_name="assistant_messages")
    op.drop_index("ix_assistant_messages_user_id", table_name="assistant_messages")
    op.drop_index("ix_assistant_messages_study_id", table_name="assistant_messages")
    op.drop_index("ix_assistant_messages_conversation_id", table_name="assistant_messages")
    op.drop_table("assistant_messages")

    op.drop_index("idx_assistant_conversations_user_updated", table_name="assistant_conversations")
    op.drop_index("idx_assistant_conversations_study_user", table_name="assistant_conversations")
    op.drop_index("ix_assistant_conversations_user_id", table_name="assistant_conversations")
    op.drop_index("ix_assistant_conversations_study_id", table_name="assistant_conversations")
    op.drop_table("assistant_conversations")
