"""Focused tests for assistant tool helpers without DB."""

from __future__ import annotations

import unittest
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from app.schemas.assistant_schema import AssistantMetric, AssistantQueryPlan, AssistantToolName, RankDirection
from app.services.assistant_tools import (
    _canonical_segment_key,
    _resolve_must_include,
    age_number_to_bucket,
    build_applied_context,
    extract_age_segment_from_text,
    resolve_age_segment_key,
    tool_explain_design,
    tool_rank_designs,
    tool_rank_elements,
    tool_study_overview,
)
from app.services.design_optimizer import OptimizerElement, build_categories_from_analysis


def _analysis_fixture():
    return {
        "dashboard_summary": {
            "uniquePanelists": 40,
            "totalResponses": 400,
            "avgRating": 3.5,
            "avgResponseTime": 1.2,
            "categoryCount": 2,
            "ratingDistribution": [{"name": "Rating 5", "value": 10}],
        },
        "analysis_settings": {"top": {"hundred": [4, 5]}},
        "Information Block": {
            "Study Title": "Demo",
            "Study Background": "https://example.com/bg.png",
            "Aspect Ratio": "9 / 16",
            "Categories": [
                {
                    "name": "Layer A",
                    "elements": [
                        {"name": "A1", "url": "https://example.com/a1.png", "layer_id": "la", "image_id": "ia1", "z_index": 0, "transform": {"x": 0, "y": 0, "width": 100, "height": 100}},
                        {"name": "A2", "url": "https://example.com/a2.png", "layer_id": "la", "image_id": "ia2", "z_index": 0, "transform": {"x": 0, "y": 0, "width": 100, "height": 100}},
                    ],
                },
                {
                    "name": "Layer B",
                    "elements": [
                        {"name": "B1", "url": "https://example.com/b1.png", "layer_id": "lb", "image_id": "ib1", "z_index": 1, "transform": {"x": 10, "y": 10, "width": 80, "height": 80}},
                        {"name": "B2", "url": "https://example.com/b2.png", "layer_id": "lb", "image_id": "ib2", "z_index": 1, "transform": {"x": 10, "y": 10, "width": 80, "height": 80}},
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


class AssistantToolsTests(unittest.TestCase):
    def test_build_categories_from_analysis(self):
        cats = build_categories_from_analysis(_analysis_fixture(), metric="T", study_type="layer")
        self.assertEqual(len(cats), 2)
        self.assertEqual(cats[0].elements[0].name, "A1")

    def test_study_overview_tool(self):
        study = SimpleNamespace(id=uuid4(), study_type="layer", title="Demo")
        plan = AssistantQueryPlan(tool=AssistantToolName.study_overview)
        ctx = build_applied_context(study, _analysis_fixture(), plan, None)
        result = tool_study_overview(_analysis_fixture(), ctx)
        self.assertIn("40 panelists", result["answer_text"])
        self.assertTrue(result["evidence"])

    def test_rank_elements_tool(self):
        study = SimpleNamespace(id=uuid4(), study_type="layer", title="Demo", design_constraints=[], layers=[])
        plan = AssistantQueryPlan(
            tool=AssistantToolName.rank_elements,
            metric=AssistantMetric.T,
            direction=RankDirection.highest,
            limit=3,
        )
        result = tool_rank_elements(_analysis_fixture(), study, plan)
        self.assertIn("highest-scoring", result["answer_text"])
        block = result["blocks"][0]
        self.assertEqual(block["type"], "top_bottom_elements")
        self.assertEqual(block["data"]["items"][0]["name"], "A1")

    def test_rank_designs_tool_stacks_by_z_index(self):
        study = SimpleNamespace(id=uuid4(), study_type="layer", title="Demo", design_constraints=[], layers=[])
        plan = AssistantQueryPlan(
            tool=AssistantToolName.rank_designs,
            metric=AssistantMetric.T,
            direction=RankDirection.highest,
            limit=5,
        )
        result = tool_rank_designs(_analysis_fixture(), study, plan)
        self.assertIn("one element from every layer", result["answer_text"])
        designs = result["blocks"][0]["data"]["designs"]
        self.assertGreaterEqual(len(designs), 1)
        top = designs[0]
        self.assertEqual(top["score"], 20)  # 12 + 8
        z_order = [el["z_index"] for el in top["elements"]]
        self.assertEqual(z_order, sorted(z_order))

    def test_age_number_maps_to_standard_buckets(self):
        self.assertEqual(age_number_to_bucket(47), "45-54")
        self.assertEqual(age_number_to_bucket(45), "45-54")
        self.assertEqual(age_number_to_bucket(54), "45-54")
        self.assertEqual(age_number_to_bucket(22), "18-24")
        self.assertEqual(age_number_to_bucket(65), "65+")
        self.assertEqual(resolve_age_segment_key("45"), "45-54")
        self.assertEqual(resolve_age_segment_key("45-50"), "45-54")
        self.assertEqual(resolve_age_segment_key("13-17"), "13-18")
        self.assertEqual(extract_age_segment_from_text("age group of 45-50"), "45-54")
        self.assertEqual(extract_age_segment_from_text("for the age of 47"), "45-54")

    def test_rank_designs_resolves_single_age_to_bucket(self):
        analysis = _analysis_fixture()
        age = deepcopy(analysis["(T) Overall"])
        age["segments"] = {"45-54": {"base_size": 12}, "25-34": {"base_size": 8}}
        for category in age["categories"]:
            for element in category["elements"]:
                element["values"] = {
                    "45-54": element["value"],
                    "25-34": element["value"] - 1,
                }
        analysis["(T) Age"] = age
        study = SimpleNamespace(id=uuid4(), study_type="layer", title="Demo", design_constraints=[], layers=[])

        plan = AssistantQueryPlan(
            tool=AssistantToolName.rank_designs,
            metric=AssistantMetric.T,
            limit=1,
            segment_section="Age",
            segment_key="47",
        )
        result = tool_rank_designs(analysis, study, plan)
        self.assertEqual(plan.segment_key, "45-54")
        self.assertTrue(result["blocks"])
        self.assertEqual(
            _canonical_segment_key(analysis, "T", "Age", "45"),
            "45-54",
        )

    def test_rank_designs_resolves_gender_alias_and_reports_missing_segment(self):
        analysis = _analysis_fixture()
        gender = deepcopy(analysis["(T) Overall"])
        gender["segments"] = {"Male": {"base_size": 2}}
        for category in gender["categories"]:
            for element in category["elements"]:
                element["values"] = {"Male": element["value"]}
        analysis["(T) Gender"] = gender
        study = SimpleNamespace(id=uuid4(), study_type="layer", title="Demo", design_constraints=[], layers=[])

        male_plan = AssistantQueryPlan(
            tool=AssistantToolName.rank_designs,
            metric=AssistantMetric.T,
            limit=1,
            segment_section="Gender",
            segment_key="men",
        )
        male_result = tool_rank_designs(analysis, study, male_plan)
        self.assertEqual(male_plan.segment_key, "Male")
        self.assertTrue(male_result["blocks"])

        female_plan = AssistantQueryPlan(
            tool=AssistantToolName.rank_designs,
            metric=AssistantMetric.T,
            limit=1,
            segment_section="Gender",
            segment_key="Female",
        )
        female_result = tool_rank_designs(analysis, study, female_plan)
        self.assertFalse(female_result["blocks"])
        self.assertIn("Available segments: Male", female_result["answer_text"])

    def test_must_include_forces_named_element_in_design(self):
        analysis = _analysis_fixture()
        study = SimpleNamespace(id=uuid4(), study_type="layer", title="Demo", design_constraints=[], layers=[])
        plan = AssistantQueryPlan(
            tool=AssistantToolName.rank_designs,
            metric=AssistantMetric.T,
            direction=RankDirection.highest,
            limit=1,
            must_include=["A2"],
        )
        result = tool_rank_designs(analysis, study, plan)
        designs = result["blocks"][0]["data"]["designs"]
        names = [el["name"] for el in designs[0]["elements"]]
        self.assertIn("A2", names)
        self.assertIn("A2", result["blocks"][0]["data"]["must_include"])

    def test_resolve_must_include_matches_color_tokens(self):
        cats = build_categories_from_analysis(_analysis_fixture(), metric="T", study_type="layer")
        original = cats[0].elements[0]
        cats[0].elements[0] = OptimizerElement(
            element_id=original.element_id,
            category_key=original.category_key,
            category_name=original.category_name,
            name="A1-white-cap",
            value=original.value,
            code=original.code,
            image_url=original.image_url,
            element_type=original.element_type,
            z_index=original.z_index,
            category_order=original.category_order,
            layer_id=original.layer_id,
            image_id=original.image_id,
            transform=original.transform,
            above_threshold=original.above_threshold,
        )
        forced, require_any, labels, unresolved, _forced_ids = _resolve_must_include(cats, ["white"])
        self.assertFalse(forced)
        self.assertTrue(require_any)
        self.assertFalse(unresolved)
        self.assertTrue(labels)

    def test_resolve_must_include_forces_specific_silver_clinical(self):
        cats = build_categories_from_analysis(_analysis_fixture(), metric="T", study_type="layer")
        a_elems = cats[0].elements
        cats[0].elements[0] = OptimizerElement(
            element_id=a_elems[0].element_id,
            category_key=a_elems[0].category_key,
            category_name=a_elems[0].category_name,
            name="A6-silver-clinical",
            value=a_elems[0].value,
            code="A6",
            image_url=a_elems[0].image_url,
            element_type=a_elems[0].element_type,
            z_index=a_elems[0].z_index,
            category_order=a_elems[0].category_order,
            layer_id=a_elems[0].layer_id,
            image_id=a_elems[0].image_id,
            transform=a_elems[0].transform,
            above_threshold=a_elems[0].above_threshold,
        )
        cats[0].elements[1] = OptimizerElement(
            element_id=a_elems[1].element_id,
            category_key=a_elems[1].category_key,
            category_name=a_elems[1].category_name,
            name="A3-silver-cap",
            value=a_elems[1].value + 5,
            code="A3",
            image_url=a_elems[1].image_url,
            element_type=a_elems[1].element_type,
            z_index=a_elems[1].z_index,
            category_order=a_elems[1].category_order,
            layer_id=a_elems[1].layer_id,
            image_id=a_elems[1].image_id,
            transform=a_elems[1].transform,
            above_threshold=a_elems[1].above_threshold,
        )
        forced, require_any, labels, unresolved, forced_ids = _resolve_must_include(
            cats, ["A6-silver-clinical"]
        )
        self.assertFalse(unresolved)
        self.assertFalse(require_any)
        self.assertEqual(forced[cats[0].key], a_elems[0].element_id)
        self.assertIn("A6-silver-clinical", labels)
        self.assertEqual(forced_ids, [a_elems[0].element_id])

        # Partial specific code still forces clinical, not any silver.
        forced2, require_any2, labels2, unresolved2, _ = _resolve_must_include(
            cats, ["silver-clinical"]
        )
        self.assertFalse(unresolved2)
        self.assertFalse(require_any2)
        self.assertEqual(forced2[cats[0].key], a_elems[0].element_id)
        self.assertIn("A6-silver-clinical", labels2)

    def test_specific_missing_element_does_not_loose_match_clinical(self):
        cats = build_categories_from_analysis(_analysis_fixture(), metric="T", study_type="layer")
        a_elems = cats[0].elements
        cats[0].elements[0] = OptimizerElement(
            element_id=a_elems[0].element_id,
            category_key=a_elems[0].category_key,
            category_name=a_elems[0].category_name,
            name="A9-teal-maximum",
            value=a_elems[0].value,
            code="A9",
            image_url=a_elems[0].image_url,
            element_type=a_elems[0].element_type,
            z_index=a_elems[0].z_index,
            category_order=a_elems[0].category_order,
            layer_id=a_elems[0].layer_id,
            image_id=a_elems[0].image_id,
            transform=a_elems[0].transform,
            above_threshold=a_elems[0].above_threshold,
        )
        cats[0].elements[1] = OptimizerElement(
            element_id=a_elems[1].element_id,
            category_key=a_elems[1].category_key,
            category_name=a_elems[1].category_name,
            name="D3-clinical-large-side",
            value=a_elems[1].value + 5,
            code="D3",
            image_url=a_elems[1].image_url,
            element_type=a_elems[1].element_type,
            z_index=a_elems[1].z_index,
            category_order=a_elems[1].category_order,
            layer_id=a_elems[1].layer_id,
            image_id=a_elems[1].image_id,
            transform=a_elems[1].transform,
            above_threshold=a_elems[1].above_threshold,
        )
        forced, require_any, labels, unresolved, _ = _resolve_must_include(
            cats, ["A6-silver-clinical"]
        )
        self.assertFalse(forced)
        self.assertFalse(require_any)
        self.assertIn("A6-silver-clinical", unresolved)
        self.assertFalse(labels)

    def test_explain_design_compares_verified_scores(self):
        study = SimpleNamespace(id=uuid4(), study_type="layer", title="Demo", design_constraints=[], layers=[])
        plan = AssistantQueryPlan(
            tool=AssistantToolName.explain_design,
            metric=AssistantMetric.T,
            direction=RankDirection.highest,
            limit=2,
        )
        result = tool_explain_design(_analysis_fixture(), study, plan)
        self.assertIn("verified element coefficients sum", result["answer_text"])
        self.assertEqual(result["blocks"][0]["type"], "design_explanation")
        self.assertGreaterEqual(result["blocks"][0]["data"]["delta"], 0)


if __name__ == "__main__":
    unittest.main()
