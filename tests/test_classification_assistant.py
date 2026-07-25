"""Tests for classification option mapping and assistant planning."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.schemas.assistant_schema import (
    AssistantFollowUpContext,
    AssistantQueryPlan,
    AssistantQueryRequest,
    AssistantToolName,
)
from app.services.assistant_service import (
    _deterministic_plan,
    _enrich_classification_plan_with_study,
    _normalize_plan_for_question,
)
from app.services.assistant_tools import (
    _map_classification_answer,
    _option_answer_maps,
    match_classification_options_in_text,
    resolve_classification_question_from_options,
    tool_classification_distribution,
)


def _study_with_questions():
    q1 = SimpleNamespace(
        order=1,
        question_text="How often do you cook meals at home?",
        answer_options=[
            {"id": "2282f2ad-4aaa-4bbb-8ccc-ddddeeee0001", "text": "Daily or almost daily"},
            {"id": "cab7304b-3aaa-4bbb-8ccc-ddddeeee0002", "text": "Several times per week"},
            {"id": "e8f9ff91-4aaa-4bbb-8ccc-ddddeeee0003", "text": "A few times per month"},
            {"id": "2b5e6654-baaa-4bbb-8ccc-ddddeeee0004", "text": "Rarely or never"},
        ],
    )
    q2 = SimpleNamespace(
        order=2,
        question_text="How often do you purchase organic food?",
        answer_options=[
            {"id": "aaaa1111-2222-3333-4444-555566667777", "text": "Always"},
            {"id": "bbbb1111-2222-3333-4444-555566667777", "text": "Sometimes"},
            # Same option label can exist on multiple questions.
            {"id": "cccc1111-2222-3333-4444-555566667777", "text": "Daily or almost daily"},
        ],
    )
    return SimpleNamespace(id=uuid4(), classification_questions=[q1, q2], title="Demo")


class ClassificationMappingTests(unittest.TestCase):
    def test_maps_stored_option_ids_to_labels(self):
        options = [
            {"id": "2282f2ad-4aaa-4bbb-8ccc-ddddeeee0001", "text": "Daily or almost daily"},
            {"id": "cab7304b-3aaa-4bbb-8ccc-ddddeeee0002", "text": "Several times per week"},
        ]
        labels, lookup = _option_answer_maps(options)
        self.assertEqual(labels[0], "Daily or almost daily")
        self.assertEqual(
            _map_classification_answer("2282f2ad-4aaa-4bbb-8ccc-ddddeeee0001", lookup),
            "Daily or almost daily",
        )
        # Truncated ids like those shown in the broken UI
        self.assertEqual(_map_classification_answer("2282f2ad-4", lookup), "Daily or almost daily")
        self.assertEqual(_map_classification_answer("cab7304b-3", lookup), "Several times per week")

    def test_option_text_uniquely_resolves_question(self):
        study = _study_with_questions()
        question, candidates = resolve_classification_question_from_options(
            study, ["A few times per month"]
        )
        self.assertIsNotNone(question)
        self.assertEqual(question.question_text, "How often do you cook meals at home?")
        self.assertEqual(candidates, [])

    def test_match_options_in_user_message(self):
        study = _study_with_questions()
        found = match_classification_options_in_text(
            study,
            "how many user answered this classification option A few times per month",
        )
        self.assertEqual(found, ["A few times per month"])


class ClassificationPlannerTests(unittest.TestCase):
    def test_option_count_question_uses_classification_tool(self):
        req = AssistantQueryRequest(
            message="how many user answered this classification option A few times per month"
        )
        plan = _deterministic_plan(req.message, req)
        self.assertEqual(plan.tool, AssistantToolName.classification_distribution)

    def test_clarification_chip_follow_up_stays_on_classification(self):
        req = AssistantQueryRequest(
            message="How often do you cook meals at home?",
            follow_up=AssistantFollowUpContext(
                last_tool=AssistantToolName.classification_distribution,
                classification_options=["A few times per month"],
            ),
        )
        plan = _deterministic_plan(req.message, req)
        plan = _normalize_plan_for_question(plan, req.message, "layer", req)
        study = _study_with_questions()
        plan = _enrich_classification_plan_with_study(plan, req.message, study, req)
        self.assertEqual(plan.tool, AssistantToolName.classification_distribution)
        self.assertEqual(plan.classification_question, "How often do you cook meals at home?")
        self.assertEqual(plan.classification_options, ["A few times per month"])

    def test_enrich_resolves_option_only_query(self):
        req = AssistantQueryRequest(message="how many selected A few times per month")
        plan = AssistantQueryPlan(tool=AssistantToolName.unsupported)
        study = _study_with_questions()
        plan = _enrich_classification_plan_with_study(plan, req.message, study, req)
        self.assertEqual(plan.tool, AssistantToolName.classification_distribution)
        self.assertEqual(plan.classification_options, ["A few times per month"])
        self.assertEqual(plan.classification_question, "How often do you cook meals at home?")

    def test_shared_option_requires_question_clarification(self):
        req = AssistantQueryRequest(message="how many selected Daily or almost daily")
        plan = AssistantQueryPlan(tool=AssistantToolName.unsupported)
        study = _study_with_questions()
        plan = _enrich_classification_plan_with_study(plan, req.message, study, req)
        self.assertEqual(plan.tool, AssistantToolName.classification_distribution)
        self.assertEqual(plan.classification_options, ["Daily or almost daily"])
        self.assertIsNone(plan.classification_question)

        result = tool_classification_distribution(MagicMock(), study, plan, None)
        self.assertEqual(result["status"], "needs_clarification")
        self.assertIn("more than one", result["answer_text"].casefold())
        self.assertGreaterEqual(len(result["clarification_options"]), 2)

    def test_segmented_classification_plan_extracts_gender_and_age(self):
        req = AssistantQueryRequest(
            message="How many selected daily or almost daily in male segment 22 years old"
        )
        plan = _normalize_plan_for_question(
            _deterministic_plan(req.message, req),
            req.message,
            "layer",
            req,
        )
        study = _study_with_questions()
        plan = _enrich_classification_plan_with_study(plan, req.message, study, req)
        self.assertEqual(plan.tool, AssistantToolName.classification_distribution)
        self.assertEqual(plan.gender_key, "Male")
        self.assertEqual(plan.age_key, "18-24")
        self.assertEqual(plan.classification_options, ["Daily or almost daily"])


class ClassificationToolTests(unittest.TestCase):
    def test_tool_maps_ids_and_focuses_option(self):
        study = _study_with_questions()
        study.study_type = "layer"
        response_id = uuid4()

        class _Scalars:
            def all(self_inner):
                return [response_id]

        class _Result:
            def scalars(self_inner):
                return _Scalars()

            def all(self_inner):
                return [
                    (response_id, "e8f9ff91-4"),
                    (response_id, "e8f9ff91-4aaa-4bbb-8ccc-ddddeeee0003"),
                ]

        calls = {"n": 0}

        def execute(_stmt):
            calls["n"] += 1
            if calls["n"] == 1:
                return _Result()
            # Second query returns answer rows; reuse same shape via .all()
            return _Result()

        db = MagicMock()
        db.execute.side_effect = execute

        plan = AssistantQueryPlan(
            tool=AssistantToolName.classification_distribution,
            classification_question="How often do you cook meals at home?",
            classification_options=["A few times per month"],
        )
        # First execute returns response ids via scalars().all()
        # Second should return answer rows via .all() — adjust mock:
        response_result = MagicMock()
        response_result.scalars.return_value.all.return_value = [response_id]
        answers_result = MagicMock()
        answers_result.all.return_value = [(response_id, "e8f9ff91-4")]
        db.execute.side_effect = [response_result, answers_result]

        result = tool_classification_distribution(db, study, plan, None)
        self.assertEqual(result["status"], "answered")
        self.assertIn("A few times per month", result["answer_text"])
        self.assertNotIn("e8f9ff91", result["answer_text"])
        options = result["blocks"][0]["data"]["options"]
        labels = [row["option"] for row in options]
        self.assertIn("A few times per month", labels)
        self.assertNotIn("e8f9ff91-4", labels)
        focused = result["blocks"][0]["data"]["focus_options"]
        self.assertEqual(focused, ["A few times per month"])
        # No duplicate chart / unused-options block
        self.assertEqual(len(result["blocks"]), 1)

    def test_tool_filters_by_gender_and_age(self):
        study = _study_with_questions()
        study.study_type = "layer"
        male_young = uuid4()
        male_old = uuid4()
        female_young = uuid4()

        response_rows = [
            (male_young, {"gender": "Male", "age": 22}),
            (male_old, {"gender": "male", "age": 47}),
            (female_young, {"gender": "Female", "age": 21}),
        ]
        answer_rows = [
            (male_young, "2282f2ad-4"),
            (male_old, "2282f2ad-4"),
            (female_young, "2282f2ad-4"),
        ]

        response_result = MagicMock()
        response_result.all.return_value = response_rows
        answers_result = MagicMock()
        answers_result.all.return_value = [(male_young, "2282f2ad-4")]
        db = MagicMock()
        db.execute.side_effect = [response_result, answers_result]

        plan = AssistantQueryPlan(
            tool=AssistantToolName.classification_distribution,
            classification_question="How often do you cook meals at home?",
            classification_options=["Daily or almost daily"],
            gender_key="Male",
            age_key="18-24",
        )
        result = tool_classification_distribution(db, study, plan, None)
        self.assertEqual(result["status"], "answered")
        self.assertIn("Male", result["answer_text"])
        self.assertIn("18-24", result["answer_text"])
        self.assertEqual(result["blocks"][0]["data"]["total_respondents"], 1)
        self.assertEqual(result["blocks"][0]["data"]["answered"], 1)
        daily = next(
            row
            for row in result["blocks"][0]["data"]["options"]
            if row["option"] == "Daily or almost daily"
        )
        self.assertEqual(daily["count"], 1)


if __name__ == "__main__":
    unittest.main()
