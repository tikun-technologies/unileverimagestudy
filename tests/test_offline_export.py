import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import numpy as np
import pandas as pd

from app.services.offline_export import (
    get_cached_offline_rows,
    invalidate_offline_rows,
    pack_dataframe,
    remember_offline_rows,
)


def unpack(packed):
    rows = [{} for _ in range(packed["count"])]
    for column in packed["columns"]:
        bits = column["bits"]
        data = base64.b64decode(column["data"])
        for index, row in enumerate(rows):
            if bits < 8:
                code = (data[index // (8 // bits)] >> ((index % (8 // bits)) * bits)) & (2 ** bits - 1)
            else:
                width = bits // 8
                code = int.from_bytes(data[index * width:(index + 1) * width], "little")
            value = column["values"][code]
            if value is not None:
                row[column["name"]] = value
    return rows


class PackedDataframeTests(unittest.TestCase):
    def test_types_missing_cells_and_embedded_images(self):
        df = pd.DataFrame({
            "Mixed": pd.Series([True, 1, "1", None], dtype=object),
            "Rating": [5, 2, np.nan, np.inf],
            "Gender": [" Female ", "Male", "", None],
            "Photo": ["data:image/png;base64,AAAA"] * 4,
            "Extra": [{"nested": True}, "x" * 4001, [], None],
        })
        packed = pack_dataframe(df)
        json.dumps(packed, allow_nan=False)
        rows = unpack(packed)
        self.assertIs(rows[0]["Mixed"], True)
        self.assertIs(type(rows[1]["Mixed"]), int)
        self.assertEqual(rows[2]["Mixed"], "1")
        self.assertEqual(rows, [
            {"Mixed": True, "Rating": 5, "Gender": "Female"},
            {"Mixed": 1, "Rating": 2, "Gender": "Male"},
            {"Mixed": "1"}, {},
        ])

    def test_matches_typescript_wire_format(self):
        self.assertEqual(pack_dataframe(pd.DataFrame({"A": [1, 0, 1], "Gender": ["F", "M", "F"]})), {
            "format": "columns-v1", "count": 3, "columns": [
                {"name": "A", "values": [None, 1, 0], "bits": 2, "data": "GQ=="},
                {"name": "Gender", "values": [None, "F", "M"], "bits": 2, "data": "GQ=="},
            ],
        })

    def test_every_index_width_and_duplicate_columns(self):
        for count, bits in [(1, 1), (3, 2), (15, 4), (255, 8), (65535, 16), (70000, 32)]:
            with self.subTest(bits=bits):
                packed = pack_dataframe(pd.DataFrame({"A": range(count)}))
                self.assertEqual(packed["columns"][0]["bits"], bits)
                self.assertEqual(unpack(packed), [{"A": value} for value in range(count)])
        self.assertEqual(unpack(pack_dataframe(pd.DataFrame([[1, 2]], columns=["A", "A"]))), [{"A": 1}])

    def test_empty_frame(self):
        self.assertEqual(pack_dataframe(pd.DataFrame(columns=["Rating"])), {
            "format": "columns-v1", "count": 0, "columns": [],
        })


class OfflineSourceEndpointTests(unittest.TestCase):
    def setUp(self):
        self.redis_patch = patch("app.services.offline_export._redis_enabled", return_value=False)
        self.redis_patch.start()
        self.addCleanup(self.redis_patch.stop)

    def test_old_dashboard_reports_without_response_rows_are_refreshed(self):
        from app.api.v1 import response as api

        study = SimpleNamespace(id=uuid4())
        report = {"dashboard_summary": {"totalResponses": 2}}
        with patch.object(api, "get_study_analysis_settings", return_value={}), \
             patch.object(api, "_recall_page_analysis", return_value=report), \
             patch.object(api, "_build_study_data_dict", return_value={}), \
             patch("app.services.offline_export.has_cached_offline_rows", return_value=False) as cached:
            self.assertIsNone(api._cached_page_analysis(MagicMock(), study, None, True))
            cached.return_value = True
            self.assertEqual(api._cached_page_analysis(MagicMock(), study, None, True), report)

    def test_reuses_dashboard_rows_without_reloading_the_dataframe(self):
        from app.api.v1 import response as api

        study = SimpleNamespace(id=uuid4(), classification_questions=[], updated_at="v1")
        frame = pd.DataFrame({"Panelist": ["p1"], "Rating": [5]})
        packed = remember_offline_rows(study, True, frame, study_data={})
        with patch.object(api, "analysis_viewer_email", return_value="user@unilever.com"), \
             patch.object(api, "_load_study_dataframe_for_analysis", side_effect=AssertionError("Export must reuse dashboard rows")), \
             patch.object(api, "get_cached_analysis_settings_response", return_value={"settings": {}}), \
             patch.object(api, "_build_study_data_dict", return_value={}):
            source = api.get_study_offline_analysis_source(access=SimpleNamespace(study=study), db=MagicMock())
        self.assertEqual(source["packedRows"], packed)

    def test_loads_completed_unfiltered_rows_without_running_analysis(self):
        from app.api.v1 import response as api

        study = SimpleNamespace(id=uuid4(), classification_questions=[SimpleNamespace(
            question_id="Q1", question_text="Brand", question_type="multiple_choice",
            is_required="Y", order=1, answer_options=[], config={}, optional_classification_question=False,
        )])
        access = SimpleNamespace(study=study)
        db = MagicMock()
        df = pd.DataFrame({"Panelist": ["constant", "constant", "mixed", "mixed"], "Rating": [5, 5, 5, 2]})
        snapshot = {"settings": {"exclude_constant_raters": True}, "max_rating": 5}
        with patch.object(api, "analysis_viewer_email", return_value="viewer@unilever.com"), \
             patch.object(api, "StudyResponseService") as responses, \
             patch.object(api, "get_cached_analysis_settings_response", return_value=snapshot), \
             patch.object(api, "_build_study_data_dict", return_value={"title": "Test"}), \
             patch.object(api.StudyAnalysisService, "generate_json_report", side_effect=AssertionError("Export must not analyse")):
            responses.return_value.get_study_dataframe.return_value = df
            source = api.get_study_offline_analysis_source(access=access, db=db)
            responses.return_value.get_study_dataframe.assert_called_once_with(
                study.id, unilever_format=True, completed_only=True,
            )
        self.assertEqual(source["packedRows"]["count"], 4)
        self.assertEqual(unpack(source["packedRows"]), df.to_dict("records"))
        self.assertEqual(source["settingsSnapshot"], snapshot)
        self.assertTrue(source["classificationQuestions"][0]["is_required"])

class OfflineRowsCacheTests(unittest.TestCase):
    def setUp(self):
        self.redis_patch = patch("app.services.offline_export._redis_enabled", return_value=False)
        self.redis_patch.start()
        self.addCleanup(self.redis_patch.stop)
        self.study = SimpleNamespace(id=uuid4(), updated_at="v1", completed_responses=2, study_type="layer")
        self.frame = pd.DataFrame({"Panelist": ["a", "b"], "Rating": [1, 5]})

    def test_format_revision_and_study_isolation(self):
        remember_offline_rows(self.study, True, self.frame)
        self.assertIsNotNone(get_cached_offline_rows(self.study, True))
        self.assertIsNone(get_cached_offline_rows(self.study, False))
        self.assertIsNone(get_cached_offline_rows(SimpleNamespace(id=uuid4()), True))
        self.study.completed_responses = 3
        self.assertIsNone(get_cached_offline_rows(self.study, True))
        self.study.completed_responses = 2
        self.study.updated_at = "v2"
        self.assertIsNone(get_cached_offline_rows(self.study, True))

    def test_expires_and_invalidates_on_response_or_study_changes(self):
        from app.core.cache import invalidate_assistant_analysis_cache, invalidate_study_cache
        remember_offline_rows(self.study, True, self.frame)
        with patch("app.services.offline_export.time.monotonic", return_value=float("inf")):
            self.assertIsNone(get_cached_offline_rows(self.study, True))

        for invalidate in [invalidate_offline_rows, invalidate_assistant_analysis_cache, invalidate_study_cache]:
            remember_offline_rows(self.study, True, self.frame)
            with patch("app.core.cache.RedisCache.delete_pattern"), patch("app.core.cache.RedisCache.delete"):
                invalidate(self.study.id)
            self.assertIsNone(get_cached_offline_rows(self.study, True))

    def test_definition_changes_do_not_reuse_previous_column_names(self):
        remember_offline_rows(self.study, True, self.frame, study_data={"categories": [{"name": "Before"}]})
        self.assertIsNone(get_cached_offline_rows(self.study, True, study_data={"categories": [{"name": "After"}]}))

    def test_redis_reads_do_not_serve_a_stale_local_copy(self):
        remember_offline_rows(self.study, True, self.frame)
        with patch("app.services.offline_export._redis_enabled", return_value=True), \
             patch("app.services.offline_export._rows_redis") as redis:
            redis.return_value.get.return_value = None
            self.assertIsNone(get_cached_offline_rows(self.study, True))

    def test_compressed_redis_payload_round_trips_large_response_data(self):
        stored = {}
        def save(key, ttl_seconds, value):
            stored[key] = value
            return True
        frame = pd.DataFrame({"Rating": [1, 5] * 50_000, "ResponseTime": range(100_000)})
        with patch("app.services.offline_export._redis_enabled", return_value=True), \
             patch("app.services.offline_export._rows_redis") as redis:
            redis.return_value.setex.side_effect = save
            redis.return_value.get.side_effect = stored.get
            packed = remember_offline_rows(self.study, True, frame)
            # Simulate a different API worker with no in-memory copy.
            from app.services.offline_export import _rows_cache
            _rows_cache.clear()
            self.assertEqual(get_cached_offline_rows(self.study, True), packed)
        self.assertLess(len(next(iter(stored.values()))), 1_000_000)

    def test_shared_invalidation_prevents_serving_an_in_memory_copy(self):
        stored = {}
        def save(key, ttl_seconds, value):
            stored[key] = value
            return True
        with patch("app.services.offline_export._redis_enabled", return_value=True), \
             patch("app.services.offline_export._rows_redis") as redis:
            redis.return_value.setex.side_effect = save
            redis.return_value.get.side_effect = stored.get
            remember_offline_rows(self.study, True, self.frame)
            self.assertIsNotNone(get_cached_offline_rows(self.study, True))
            redis.return_value.get.reset_mock()
            self.assertIsNotNone(get_cached_offline_rows(self.study, True))
            self.assertEqual(redis.return_value.get.call_count, 1)
            stored.clear()
            self.assertIsNone(get_cached_offline_rows(self.study, True))


if __name__ == "__main__":
    unittest.main()
