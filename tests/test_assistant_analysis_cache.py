"""Tests for assistant analysis caching + cache-key isolation."""

from __future__ import annotations

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import app.services.assistant_tools as tools


class AnalysisCacheTests(unittest.TestCase):
    def setUp(self):
        tools._ANALYSIS_MEMO.clear()

    def _study(self):
        return SimpleNamespace(id=uuid.uuid4(), study_type="layer", title="S")

    def _run(self, study, user, filters, report, analysis_settings=None):
        report_holder = {"report": report}
        fake_service = MagicMock()
        fake_service.get_study_dataframe.return_value = MagicMock()  # df placeholder
        analysis_service = MagicMock()
        analysis_service.generate_json_report.return_value = report_holder["report"]

        with patch.object(tools, "StudyResponseService", return_value=fake_service) as svc, patch.object(
            tools, "get_study_analysis_settings", return_value=analysis_settings or {}
        ), patch.object(
            tools, "_build_study_data_dict", return_value={}
        ), patch.object(
            tools, "StudyAnalysisService", return_value=analysis_service
        ), patch.object(
            tools, "is_unilever_domain", return_value=False
        ), patch.object(
            tools.RedisCache, "get", return_value=None
        ), patch.object(
            tools.RedisCache, "set", return_value=True
        ):
            result = tools.load_analysis_for_assistant(
                MagicMock(), study, user, filters=filters
            )
            return result, svc, analysis_service

    def test_second_identical_call_uses_memo_not_rebuild(self):
        study = self._study()
        user = SimpleNamespace(email="a@x.com", id=uuid.uuid4())
        report = {"dashboard_summary": {"totalResponses": 10}}

        result1, svc1, analysis1 = self._run(study, user, None, report)
        self.assertEqual(result1, report)
        analysis1.generate_json_report.assert_called_once()

        # Second identical call should hit the in-process memo → no rebuild.
        fake_service = MagicMock()
        analysis_service = MagicMock()
        with patch.object(tools, "StudyResponseService", return_value=fake_service), patch.object(
            tools, "get_study_analysis_settings", return_value={}
        ), patch.object(tools, "_build_study_data_dict", return_value={}), patch.object(
            tools, "StudyAnalysisService", return_value=analysis_service
        ), patch.object(tools, "is_unilever_domain", return_value=False), patch.object(
            tools.RedisCache, "get", return_value=None
        ), patch.object(tools.RedisCache, "set", return_value=True):
            result2 = tools.load_analysis_for_assistant(MagicMock(), study, user, filters=None)

        self.assertEqual(result2, report)
        fake_service.get_study_dataframe.assert_not_called()
        analysis_service.generate_json_report.assert_not_called()

    def test_different_filters_do_not_collide(self):
        study = self._study()
        user = SimpleNamespace(email="a@x.com", id=uuid.uuid4())

        r_none, _, _ = self._run(study, user, None, {"tag": "overall"})
        r_male, _, a_male = self._run(
            study, user, {"gender": ["Male"]}, {"tag": "male"}
        )
        self.assertEqual(r_none, {"tag": "overall"})
        self.assertEqual(r_male, {"tag": "male"})
        # A different filter must rebuild (not reuse the "overall" memo).
        a_male.generate_json_report.assert_called_once()

    def test_use_cache_false_bypasses_memo(self):
        study = self._study()
        user = SimpleNamespace(email="a@x.com", id=uuid.uuid4())
        self._run(study, user, None, {"tag": "first"})

        fake_service = MagicMock()
        analysis_service = MagicMock()
        analysis_service.generate_json_report.return_value = {"tag": "fresh"}
        with patch.object(tools, "StudyResponseService", return_value=fake_service), patch.object(
            tools, "get_study_analysis_settings", return_value={}
        ), patch.object(tools, "_build_study_data_dict", return_value={}), patch.object(
            tools, "StudyAnalysisService", return_value=analysis_service
        ), patch.object(tools, "is_unilever_domain", return_value=False), patch.object(
            tools.RedisCache, "get", return_value=None
        ), patch.object(tools.RedisCache, "set", return_value=True):
            result = tools.load_analysis_for_assistant(
                MagicMock(), study, user, filters=None, use_cache=False
            )
        self.assertEqual(result, {"tag": "fresh"})
        analysis_service.generate_json_report.assert_called_once()

    def test_different_analysis_settings_do_not_collide(self):
        """
        Regression: toggling a setting like "with/without intercept" must bust the
        cache immediately, not silently keep serving numbers computed under the
        old setting until the TTL happens to expire.
        """
        study = self._study()
        user = SimpleNamespace(email="a@x.com", id=uuid.uuid4())

        r_without, _, a_without = self._run(
            study, user, None, {"tag": "without_intercept"},
            analysis_settings={"use_intercept": False},
        )
        r_with, _, a_with = self._run(
            study, user, None, {"tag": "with_intercept"},
            analysis_settings={"use_intercept": True},
        )

        self.assertEqual(r_without, {"tag": "without_intercept"})
        self.assertEqual(r_with, {"tag": "with_intercept"})
        # Same study, same filters — only the settings differ. Both must have
        # actually rebuilt rather than one reusing the other's cached report.
        a_without.generate_json_report.assert_called_once()
        a_with.generate_json_report.assert_called_once()

    def test_analysis_cache_key_changes_with_settings(self):
        study_id = uuid.uuid4()
        key_a = tools._analysis_cache_key(study_id, False, None, {"use_intercept": False})
        key_b = tools._analysis_cache_key(study_id, False, None, {"use_intercept": True})
        key_same = tools._analysis_cache_key(study_id, False, None, {"use_intercept": False})
        self.assertNotEqual(key_a, key_b)
        self.assertEqual(key_a, key_same)

    def test_different_email_domain_isolated(self):
        study = self._study()
        std_user = SimpleNamespace(email="a@x.com", id=uuid.uuid4())
        uni_user = SimpleNamespace(email="a@unilever.com", id=uuid.uuid4())

        key_std = tools._analysis_cache_key(study.id, False, None)
        key_uni = tools._analysis_cache_key(study.id, True, None)
        self.assertNotEqual(key_std, key_uni)


if __name__ == "__main__":
    unittest.main()
