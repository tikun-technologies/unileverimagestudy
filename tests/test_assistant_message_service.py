"""Unit tests for private assistant chat persistence + keyset pagination."""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.schemas.assistant_schema import (
    AppliedContext,
    AssistantFollowUpContext,
    AssistantQueryResponse,
)
from app.services.assistant_message_service import (
    AssistantMessageServiceError,
    decode_cursor,
    encode_cursor,
    list_messages_page,
)


def _msg(
    *,
    conversation_id,
    role="user",
    content="hi",
    created_at=None,
    message_id=None,
    client_message_id=None,
    parent_message_id=None,
    response_payload=None,
    status="complete",
    study_id=None,
    user_id=None,
):
    return SimpleNamespace(
        id=message_id or uuid.uuid4(),
        conversation_id=conversation_id,
        study_id=study_id or uuid.uuid4(),
        user_id=user_id or uuid.uuid4(),
        role=role,
        content=content,
        created_at=created_at or datetime.now(timezone.utc),
        client_message_id=client_message_id,
        parent_message_id=parent_message_id,
        response_payload=response_payload,
        status=status,
    )


class CursorCodecTests(unittest.TestCase):
    def test_round_trip(self):
        message_id = uuid.uuid4()
        created_at = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
        cursor = encode_cursor(created_at, message_id)
        decoded = decode_cursor(cursor)
        self.assertIsNotNone(decoded)
        ts, mid = decoded
        self.assertEqual(mid, message_id)
        self.assertEqual(ts, created_at)

    def test_invalid_cursor_raises(self):
        with self.assertRaises(AssistantMessageServiceError):
            decode_cursor("not-a-valid-cursor!!!")


class ListMessagesPageTests(unittest.TestCase):
    def test_empty_conversation_returns_fast_empty_page(self):
        db = MagicMock()
        db.scalar.return_value = None
        page = list_messages_page(
            db,
            study_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            limit=20,
        )
        self.assertEqual(page.items, [])
        self.assertFalse(page.meta.has_more)
        self.assertIsNone(page.meta.next_cursor)
        self.assertIsNone(page.meta.conversation_id)

    def test_newest_page_reversed_and_has_more_cursor(self):
        conversation_id = uuid.uuid4()
        study_id = uuid.uuid4()
        user_id = uuid.uuid4()
        conversation = SimpleNamespace(
            id=conversation_id,
            study_id=study_id,
            user_id=user_id,
            follow_up_context={"last_tool": "rank_designs"},
        )
        base = datetime(2026, 7, 29, 10, 0, 0, tzinfo=timezone.utc)
        # Queried DESC: newest first. Service should reverse for UI.
        newest = _msg(
            conversation_id=conversation_id,
            content="newest",
            created_at=base + timedelta(seconds=2),
            role="assistant",
            response_payload={
                "request_id": "r1",
                "status": "answered",
                "answer_text": "newest",
                "applied_context": {
                    "study_id": str(study_id),
                    "study_type": "layer",
                },
                "blocks": [],
                "evidence": [],
                "follow_ups": [],
                "actions": [],
                "clarification_options": [],
            },
            study_id=study_id,
            user_id=user_id,
        )
        older = _msg(
            conversation_id=conversation_id,
            content="older",
            created_at=base + timedelta(seconds=1),
            role="user",
            client_message_id="c1",
            study_id=study_id,
            user_id=user_id,
        )
        oldest_extra = _msg(
            conversation_id=conversation_id,
            content="extra-for-has-more",
            created_at=base,
            role="user",
            client_message_id="c0",
            study_id=study_id,
            user_id=user_id,
        )

        db = MagicMock()
        db.scalar.return_value = conversation
        # LIMIT+1 rows in DESC order
        db.scalars.return_value.all.return_value = [newest, older, oldest_extra]

        page = list_messages_page(
            db,
            study_id=study_id,
            user_id=user_id,
            limit=2,
        )
        self.assertEqual([item.content for item in page.items], ["older", "newest"])
        self.assertTrue(page.meta.has_more)
        self.assertIsNotNone(page.meta.next_cursor)
        self.assertEqual(page.meta.conversation_id, conversation_id)
        self.assertIsNotNone(page.follow_up_context)
        self.assertEqual(page.follow_up_context.last_tool.value, "rank_designs")

        # Cursor points at the oldest item in the returned page.
        ts, mid = decode_cursor(page.meta.next_cursor)
        self.assertEqual(mid, older.id)
        self.assertEqual(ts, older.created_at)

    def test_identical_timestamps_use_id_tiebreak_in_cursor(self):
        conversation_id = uuid.uuid4()
        study_id = uuid.uuid4()
        user_id = uuid.uuid4()
        conversation = SimpleNamespace(
            id=conversation_id,
            study_id=study_id,
            user_id=user_id,
            follow_up_context=None,
        )
        same_ts = datetime(2026, 7, 29, 11, 0, 0, tzinfo=timezone.utc)
        id_a = uuid.UUID("00000000-0000-0000-0000-00000000000a")
        id_b = uuid.UUID("00000000-0000-0000-0000-00000000000b")
        # DESC by (created_at, id): b then a
        msg_b = _msg(
            conversation_id=conversation_id,
            content="b",
            created_at=same_ts,
            message_id=id_b,
            study_id=study_id,
            user_id=user_id,
        )
        msg_a = _msg(
            conversation_id=conversation_id,
            content="a",
            created_at=same_ts,
            message_id=id_a,
            study_id=study_id,
            user_id=user_id,
        )
        db = MagicMock()
        db.scalar.return_value = conversation
        db.scalars.return_value.all.return_value = [msg_b, msg_a]

        page = list_messages_page(db, study_id=study_id, user_id=user_id, limit=2)
        self.assertEqual([item.content for item in page.items], ["a", "b"])
        self.assertFalse(page.meta.has_more)


class IsolationScopeTests(unittest.TestCase):
    def test_list_filters_by_study_and_user_conversation(self):
        """list_messages_page must look up conversation by both study_id and user_id."""
        db = MagicMock()
        db.scalar.return_value = None
        study_id = uuid.uuid4()
        user_id = uuid.uuid4()
        list_messages_page(db, study_id=study_id, user_id=user_id, limit=10)
        # Ensure a DB lookup happened (conversation scoped query).
        self.assertTrue(db.scalar.called)


class ResponsePayloadRestoreTests(unittest.TestCase):
    def test_assistant_history_item_restores_response(self):
        conversation_id = uuid.uuid4()
        study_id = uuid.uuid4()
        user_id = uuid.uuid4()
        conversation = SimpleNamespace(
            id=conversation_id,
            study_id=study_id,
            user_id=user_id,
            follow_up_context=None,
        )
        parent_id = uuid.uuid4()
        assistant_id = uuid.uuid4()
        payload = AssistantQueryResponse(
            request_id="req",
            status="answered",
            answer_text="Best design is X",
            applied_context=AppliedContext(study_id=study_id, study_type="layer"),
            blocks=[{"type": "designs", "title": "Top", "data": {"items": []}}],
        )
        msg = _msg(
            conversation_id=conversation_id,
            role="assistant",
            content="Best design is X",
            message_id=assistant_id,
            parent_message_id=parent_id,
            response_payload=payload.model_dump(mode="json"),
            study_id=study_id,
            user_id=user_id,
        )
        db = MagicMock()
        db.scalar.return_value = conversation
        db.scalars.return_value.all.return_value = [msg]

        page = list_messages_page(db, study_id=study_id, user_id=user_id, limit=5)
        self.assertEqual(len(page.items), 1)
        item = page.items[0]
        self.assertIsNotNone(item.response)
        self.assertEqual(item.response.answer_text, "Best design is X")
        self.assertEqual(item.response.assistant_message_id, assistant_id)
        self.assertEqual(item.response.user_message_id, parent_id)


if __name__ == "__main__":
    unittest.main()
