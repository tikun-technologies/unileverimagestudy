import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.analytics_share import (
    create_or_get_active_share,
    get_active_share_for_study,
    get_share_by_token,
    revoke_active_share,
)


class TestAnalyticsShareService(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.study_id = uuid4()
        self.user_id = uuid4()

    def test_create_returns_existing_active_share(self):
        existing = MagicMock()
        existing.revoked_at = None
        existing.token = "existing-token"
        self.db.query.return_value.filter.return_value.first.return_value = existing

        result = create_or_get_active_share(self.db, self.study_id, self.user_id)
        self.assertIs(result, existing)
        self.db.add.assert_not_called()

    def test_get_share_by_token_trims_and_ignores_blank(self):
        self.assertIsNone(get_share_by_token(self.db, "  "))
        self.db.query.assert_not_called()

    def test_revoke_returns_false_when_missing(self):
        self.db.query.return_value.filter.return_value.first.return_value = None
        self.assertFalse(revoke_active_share(self.db, self.study_id))

    def test_revoke_marks_active_share(self):
        share = MagicMock()
        share.revoked_at = None
        self.db.query.return_value.filter.return_value.first.return_value = share

        self.assertTrue(revoke_active_share(self.db, self.study_id))
        self.assertIsNotNone(share.revoked_at)
        self.assertIsNone(share.current_filters)
        self.db.commit.assert_called()

    def test_get_active_share_filters_unrevoked(self):
        share = MagicMock()
        self.db.query.return_value.filter.return_value.first.return_value = share
        self.assertIs(get_active_share_for_study(self.db, self.study_id), share)


if __name__ == "__main__":
    unittest.main()
