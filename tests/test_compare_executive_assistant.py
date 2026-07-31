"""Tests for compare mode, classification cohort ranking, and executive summary."""

from __future__ import annotations

import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.schemas.assistant_schema import (
    AssistantMetric,
    AssistantQueryPlan,
    AssistantQueryRequest,
    AssistantToolName,
    CompareMode,
    RankDirection,
)
from app.services.assistant_service import (
    _deterministic_plan,
    _enrich_classification_plan_with_study,
    _is_cohort_rank_query,
    _is_executive_summary_query,
    _normalize_plan_for_question,
)
from app.services.assistant_tools import (
    extract_compare_pair,
    infer_compare_mode,
    is_best_vs_worst_design_query,
    is_compare_intent,
    is_compare_top_designs_query,
    resolve_classification_option_hint,
    tool_compare,
    tool_executive_summary,
    tool_rank_designs,
)


def _analysis_fixture():
    base = {
        "dashboard_summary": {
            "uniquePanelists": 40,
            "totalResponses": 400,
            "avgRating": 3.5,
            "avgResponseTime": 1.2,
            "categoryCount": 2,
            "ratingDistribution": [{"name": "Rating 5", "value": 10}],
        },
        "Information Block": {
            "Study Title": "Demo",
            "Study Background": "https://example.com/bg.png",
            "Aspect Ratio": "9 / 16",
            "Categories": [
                {
                    "name": "Layer A",
                    "elements": [
                        {"name": "A1", "url": "https://example.com/a1.png", "layer_id": "la", "image_id": "ia1", "z_index": 0},
                        {"name": "A2", "url": "https://example.com/a2.png", "layer_id": "la", "image_id": "ia2", "z_index": 0},
                    ],
                },
                {
                    "name": "Layer B",
                    "elements": [
                        {"name": "B1", "url": "https://example.com/b1.png", "layer_id": "lb", "image_id": "ib1", "z_index": 1},
                        {"name": "B2", "url": "https://example.com/b2.png", "layer_id": "lb", "image_id": "ib2", "z_index": 1},
                    ],
                },
            ],
        },
        "(T) Overall": {
            "base_size": 40,
            "threshold": 5,
            "categories": [
                {
                    "name": "Layer A",
                    "z_index": 0,
                    "elements": [
                        {"code": "A1", "name": "A1", "value": 12, "above_threshold": True, "layer_id": "la", "image_id": "ia1"},
                        {"code": "A2", "name": "A2", "value": -3, "above_threshold": False, "layer_id": "la", "image_id": "ia2"},
                    ],
                },
                {
                    "name": "Layer B",
                    "z_index": 1,
                    "elements": [
                        {"code": "B1", "name": "B1", "value": 8, "above_threshold": True, "layer_id": "lb", "image_id": "ib1"},
                        {"code": "B2", "name": "B2", "value": 1, "above_threshold": False, "layer_id": "lb", "image_id": "ib2"},
                    ],
                },
            ],
        },
    }
    gender = deepcopy(base["(T) Overall"])
    gender["segments"] = {"Male": {"base_size": 20}, "Female": {"base_size": 18}}
    for category in gender["categories"]:
        for element in category["elements"]:
            element["values"] = {
                "Male": element["value"],
                "Female": element["value"] - 2,
            }
    base["(T) Gender"] = gender
    age = deepcopy(base["(T) Overall"])
    age["segments"] = {"18-24": {"base_size": 10}, "45-54": {"base_size": 12}}
    for category in age["categories"]:
        for element in category["elements"]:
            element["values"] = {
                "18-24": element["value"],
                "45-54": element["value"] + 3,
            }
    base["(T) Age"] = age
    return base


def _study_with_questions():
    q1 = SimpleNamespace(
        order=1,
        question_text="How often do you cook meals at home?",
        answer_options=[
            {"id": "1", "text": "Daily or almost daily"},
            {"id": "2", "text": "Rarely or never"},
        ],
    )
    return SimpleNamespace(
        id=uuid4(),
        study_type="layer",
        title="Demo",
        design_constraints=[],
        layers=[],
        classification_questions=[q1],
    )


class CompareExecutivePlannerTests(unittest.TestCase):
    def test_executive_summary_query_detection(self):
        self.assertTrue(_is_executive_summary_query("give me the 5 most important findings from this study"))
        self.assertFalse(_is_executive_summary_query("show study overview"))

    def test_cohort_rank_query_detection(self):
        self.assertTrue(
            _is_cohort_rank_query(
                "show best design for people who selected daily or almost daily"
            )
        )
        self.assertFalse(_is_cohort_rank_query("how many selected daily or almost daily"))

    def test_compare_pair_extraction(self):
        pair = extract_compare_pair("Best design for males vs females")
        self.assertEqual(pair, ("Best design for males", "females"))
        # "and" must work for male/female best-design compares.
        pair_and = extract_compare_pair("compare male best design and female best design")
        self.assertEqual(pair_and, ("male best design", "female best design"))

    def test_male_best_vs_female_best_not_design_ranks(self):
        for msg in (
            "compare male best design and female best design",
            "compare male best design vs female best design",
            "Best design for males vs females",
            "difference between male best design and female best design",
            "difference between male bestie and female bestie",
            "how does male best design differ from female best design",
            "male best compared to female best",
        ):
            self.assertTrue(is_compare_intent(msg), msg)
            self.assertFalse(is_compare_top_designs_query(msg), msg)
            req = AssistantQueryRequest(message=msg)
            plan = _normalize_plan_for_question(
                _deterministic_plan(req.message, req),
                req.message,
                "layer",
                req,
            )
            self.assertEqual(plan.tool, AssistantToolName.compare, msg)
            self.assertEqual(plan.compare_mode, CompareMode.segment, msg)
            self.assertEqual(plan.compare_left, "Male", msg)
            self.assertEqual(plan.compare_right, "Female", msg)
            self.assertIsNone(plan.gender_key, msg)
            # Must never collapse to Design #1 vs #2.
            self.assertNotIn(plan.compare_left, {"design 1", "1"}, msg)

    def test_difference_between_best_and_worst(self):
        for msg in (
            "difference between best and worst design",
            "what's the difference between the best and worst design",
        ):
            self.assertTrue(is_compare_intent(msg), msg)
            self.assertTrue(is_best_vs_worst_design_query(msg), msg)
            req = AssistantQueryRequest(message=msg)
            plan = _normalize_plan_for_question(
                _deterministic_plan(req.message, req),
                req.message,
                "layer",
                req,
            )
            self.assertEqual(plan.tool, AssistantToolName.compare, msg)
            self.assertEqual(plan.compare_left, "best", msg)
            self.assertEqual(plan.compare_right, "worst", msg)

    def test_deterministic_plan_routes_compare(self):
        req = AssistantQueryRequest(message="Compare Male vs Female best designs")
        plan = _deterministic_plan(req.message, req)
        self.assertEqual(plan.tool, AssistantToolName.compare)
        # Dual-gender compare is normalized to clean segment labels.
        self.assertEqual(plan.compare_mode, CompareMode.segment)
        self.assertEqual(plan.compare_left, "Male")
        self.assertEqual(plan.compare_right, "Female")

    def test_deterministic_plan_routes_executive_summary(self):
        req = AssistantQueryRequest(message="Give me the 5 most important findings from this study")
        plan = _deterministic_plan(req.message, req)
        self.assertEqual(plan.tool, AssistantToolName.executive_summary)

    def test_cohort_rank_enrichment_keeps_rank_designs(self):
        study = _study_with_questions()
        req = AssistantQueryRequest(
            message="Show best design for people who selected Daily or almost daily"
        )
        plan = _normalize_plan_for_question(
            _deterministic_plan(req.message, req),
            req.message,
            "layer",
            req,
        )
        plan = _enrich_classification_plan_with_study(plan, req.message, study, req)
        self.assertEqual(plan.tool, AssistantToolName.rank_designs)
        self.assertEqual(plan.classification_options, ["Daily or almost daily"])
        self.assertEqual(plan.classification_question, "How often do you cook meals at home?")


class CompareToolTests(unittest.TestCase):
    def test_infer_compare_mode_gender(self):
        study = _study_with_questions()
        plan = AssistantQueryPlan(
            tool=AssistantToolName.compare,
            compare_left="Male",
            compare_right="Female",
        )
        mode, left, right = infer_compare_mode("Male vs Female", study, plan)
        self.assertEqual(mode, CompareMode.segment)
        self.assertEqual(left, "Male")
        self.assertEqual(right, "Female")

    def test_compare_gender_segments_side_by_side(self):
        study = SimpleNamespace(id=uuid4(), study_type="layer", title="Demo", design_constraints=[], layers=[])
        plan = AssistantQueryPlan(
            tool=AssistantToolName.compare,
            metric=AssistantMetric.T,
            compare_left="Male",
            compare_right="Female",
        )
        user = SimpleNamespace(email="test@example.com")
        result = tool_compare(
            MagicMock(),
            _analysis_fixture(),
            study,
            user,
            plan,
            None,
            "Male vs Female",
        )
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["blocks"][0]["type"], "side_by_side_compare")
        self.assertIn("Male", result["answer_text"])
        self.assertIn("Female", result["answer_text"])

    def test_compare_design_numbers(self):
        study = SimpleNamespace(id=uuid4(), study_type="layer", title="Demo", design_constraints=[], layers=[])
        plan = AssistantQueryPlan(
            tool=AssistantToolName.compare,
            metric=AssistantMetric.T,
            compare_left="design 1",
            compare_right="design 2",
        )
        user = SimpleNamespace(email="test@example.com")
        result = tool_compare(
            MagicMock(),
            _analysis_fixture(),
            study,
            user,
            plan,
            None,
            "Design #1 vs Design #2",
        )
        self.assertEqual(result["status"], "answered")
        self.assertIn("Design #", result["answer_text"])

    def test_compare_top_two_designs_side_by_side(self):
        study = SimpleNamespace(id=uuid4(), study_type="layer", title="Demo", design_constraints=[], layers=[])
        plan = AssistantQueryPlan(
            tool=AssistantToolName.compare,
            metric=AssistantMetric.T,
            compare_mode=CompareMode.design,
            compare_left="design 1",
            compare_right="design 2",
            limit=2,
        )
        user = SimpleNamespace(email="test@example.com")
        result = tool_compare(
            MagicMock(),
            _analysis_fixture(),
            study,
            user,
            plan,
            None,
            "compare top two designs",
        )
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["blocks"][0]["type"], "side_by_side_compare")
        self.assertEqual(result["blocks"][0]["data"]["left"]["label"], "Design #1")
        self.assertEqual(result["blocks"][0]["data"]["right"]["label"], "Design #2")
        # Must not dump a 4-item ranking list.
        self.assertFalse(any(b.get("type") == "top_k_designs" for b in result["blocks"]))

    def test_best_vs_worst_design_query_detection(self):
        self.assertTrue(is_best_vs_worst_design_query("compare best and worst design"))
        self.assertTrue(is_best_vs_worst_design_query("best vs worst design"))
        # "top" is a synonym for best — must not fall through to Design #1 vs #2.
        self.assertTrue(is_best_vs_worst_design_query("compare top and worst design"))
        self.assertTrue(is_best_vs_worst_design_query("Compare top and worst performing design"))
        self.assertTrue(is_best_vs_worst_design_query("compare top vs worst design"))
        self.assertFalse(is_best_vs_worst_design_query("best design for males vs females"))
        self.assertFalse(is_best_vs_worst_design_query("compare top two designs"))

    def test_infer_compare_mode_best_worst(self):
        study = _study_with_questions()
        plan = AssistantQueryPlan(tool=AssistantToolName.compare)
        mode, left, right = infer_compare_mode(
            "compare best and worst design", study, plan
        )
        self.assertEqual(mode, CompareMode.design)
        self.assertEqual(left, "best")
        self.assertEqual(right, "worst")

        mode2, left2, right2 = infer_compare_mode(
            "compare top and worst design", study, plan
        )
        self.assertEqual(mode2, CompareMode.design)
        self.assertEqual(left2, "best")
        self.assertEqual(right2, "worst")

    def test_compare_best_and_worst_designs(self):
        study = SimpleNamespace(id=uuid4(), study_type="layer", title="Demo", design_constraints=[], layers=[])
        plan = AssistantQueryPlan(
            tool=AssistantToolName.compare,
            metric=AssistantMetric.T,
            compare_mode=CompareMode.design,
            compare_left="best",
            compare_right="worst",
        )
        user = SimpleNamespace(email="test@example.com")
        result = tool_compare(
            MagicMock(),
            _analysis_fixture(),
            study,
            user,
            plan,
            None,
            "compare best and worst design",
        )
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["blocks"][0]["type"], "side_by_side_compare")
        left = result["blocks"][0]["data"]["left"]
        right = result["blocks"][0]["data"]["right"]
        self.assertEqual(left["label"], "Best design")
        self.assertEqual(right["label"], "Worst design")
        self.assertGreaterEqual(left["top_design"]["score"], right["top_design"]["score"])

    def test_deterministic_plan_routes_best_worst(self):
        req = AssistantQueryRequest(message="Compare best and worst design")
        plan = _deterministic_plan(req.message, req)
        self.assertEqual(plan.tool, AssistantToolName.compare)
        self.assertEqual(plan.compare_left, "best")
        self.assertEqual(plan.compare_right, "worst")

    def test_deterministic_plan_routes_top_and_worst(self):
        for msg in (
            "compare top and worst design",
            "Compare top and worst performing design",
            "compare top vs worst design",
        ):
            req = AssistantQueryRequest(message=msg)
            plan = _normalize_plan_for_question(
                _deterministic_plan(req.message, req),
                req.message,
                "layer",
                req,
            )
            self.assertEqual(plan.tool, AssistantToolName.compare, msg)
            self.assertEqual(plan.compare_mode, CompareMode.design, msg)
            self.assertEqual(plan.compare_left, "best", msg)
            self.assertEqual(plan.compare_right, "worst", msg)
            # Must NOT collapse into Design #1 vs Design #2.
            self.assertNotEqual(plan.compare_left, "design 1", msg)

    def test_compare_top_two_designs_not_rank_list(self):
        self.assertTrue(is_compare_top_designs_query("compare top two designs"))
        req = AssistantQueryRequest(message="compare top two designs")
        plan = _normalize_plan_for_question(
            _deterministic_plan(req.message, req),
            req.message,
            "layer",
            req,
        )
        self.assertEqual(plan.tool, AssistantToolName.compare)
        self.assertEqual(plan.compare_mode, CompareMode.design)
        self.assertEqual(plan.compare_left, "design 1")
        self.assertEqual(plan.compare_right, "design 2")
        self.assertEqual(plan.limit, 2)

    def test_ai_wrong_rank_designs_forced_to_compare_top_two(self):
        """If the model wrongly picks rank_designs, normalize still honors compare."""
        req = AssistantQueryRequest(message="compare top two designs")
        wrong = AssistantQueryPlan(
            tool=AssistantToolName.rank_designs,
            limit=4,
            direction=RankDirection.highest,
        )
        plan = _normalize_plan_for_question(wrong, req.message, "layer", req)
        self.assertEqual(plan.tool, AssistantToolName.compare)
        self.assertEqual(plan.compare_left, "design 1")
        self.assertEqual(plan.compare_right, "design 2")

    def test_executive_summary_returns_up_to_five_bullets(self):
        study = _study_with_questions()
        plan = AssistantQueryPlan(tool=AssistantToolName.executive_summary, metric=AssistantMetric.T)
        ctx = SimpleNamespace(study_type="layer", study_title="Demo")
        db = MagicMock()
        db.execute.return_value.all.return_value = []
        result = tool_executive_summary(db, _analysis_fixture(), study, plan, ctx)
        self.assertEqual(result["status"], "answered")
        bullets = result["blocks"][0]["data"]["bullets"]
        self.assertGreaterEqual(len(bullets), 2)
        self.assertLessEqual(len(bullets), 5)
        self.assertEqual(result["blocks"][0]["type"], "executive_summary")

    def test_partial_option_hint_resolves(self):
        study = _study_with_questions()
        self.assertEqual(
            resolve_classification_option_hint(study, "rarely"),
            "Rarely or never",
        )


class CohortRankExecuteTests(unittest.TestCase):
    @patch("app.services.assistant_tools.load_analysis_for_assistant")
    def test_execute_rank_designs_applies_cohort_filter(self, load_mock):
        from app.services.assistant_tools import execute_tool
        from app.schemas.assistant_schema import AppliedContext

        analysis = _analysis_fixture()
        load_mock.return_value = analysis
        study = _study_with_questions()
        user = SimpleNamespace(email="test@example.com")
        plan = AssistantQueryPlan(
            tool=AssistantToolName.rank_designs,
            metric=AssistantMetric.T,
            limit=1,
            classification_question="How often do you cook meals at home?",
            classification_options=["Daily or almost daily"],
        )
        ctx = AppliedContext(study_id=study.id, study_type="layer", verified=True)
        result = execute_tool(
            db=MagicMock(),
            study_obj=study,
            current_user=user,
            plan=plan,
            analysis=analysis,
            filters=None,
            context=ctx,
            message="best design for people who selected Daily or almost daily",
        )
        self.assertTrue(load_mock.called)
        self.assertIn("blocks", result)
        actions = result.get("actions") or []
        self.assertTrue(any(a.get("type") == "apply_filter" for a in actions))


if __name__ == "__main__":
    unittest.main()
