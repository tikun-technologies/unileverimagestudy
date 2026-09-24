"""Tests for Combined-table lookups used by the analytics assistant."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from uuid import uuid4

from app.schemas.assistant_schema import AssistantMetric, AssistantQueryPlan, AssistantToolName
from app.services.assistant_combined import (
    compact_combined,
    execute_combined_plan,
    query_combined,
)


def _combined_analysis():
    return {
        "dashboard_summary": {"uniquePanelists": 40, "totalResponses": 400},
        "Information Block": {"Study Title": "Demo", "Aspect Ratio": "9 / 16"},
        "(T) Combined": {
            "base_size": 40,
            "threshold": 5,
            "segments": {
                "Overall": {"base_size": 40},
                "Gender": {"Male": {"base_size": 22}, "Female": {"base_size": 18}},
                "Age": {"18-24": {"base_size": 12}},
                "Classification": {
                    "How often": {
                        "answers": {"Daily": {"base_size": 10}},
                    }
                },
            },
            "categories": [
                {
                    "code": "A",
                    "name": "Headline",
                    "elements": [
                        {
                            "code": "A1",
                            "name": "Kills 99.9% of germs",
                            "values": {
                                "Overall": 12,
                                "Gender::Male": 15,
                                "Gender::Female": 6,
                                "Age::18-24": 14,
                                "Classification::How often::Daily": 11,
                            },
                            "above_threshold": {"Overall": True},
                        },
                        {
                            "code": "A2",
                            "name": "Gentle on skin",
                            "values": {
                                "Overall": -3,
                                "Gender::Male": -1,
                                "Gender::Female": 5,
                                "Age::18-24": 0,
                                "Classification::How often::Daily": 2,
                            },
                            "above_threshold": {"Overall": False},
                        },
                    ],
                },
                {
                    "code": "B",
                    "name": "Pack shot",
                    "elements": [
                        {
                            "code": "B1",
                            "name": "Silver bottle",
                            "values": {
                                "Overall": 8,
                                "Gender::Male": 4,
                                "Gender::Female": 11,
                                "Age::18-24": 9,
                                "Classification::How often::Daily": 7,
                            },
                            "above_threshold": {"Overall": True},
                        },
                    ],
                },
            ],
        },
        "(T) Overall": {
            "base_size": 40,
            "threshold": 5,
            "categories": [
                {
                    "code": "A",
                    "name": "Headline",
                    "elements": [
                        {"code": "A1", "name": "Kills 99.9% of germs", "value": 12, "above_threshold": True},
                        {"code": "A2", "name": "Gentle on skin", "value": -3, "above_threshold": False},
                    ],
                },
                {
                    "code": "B",
                    "name": "Pack shot",
                    "elements": [
                        {"code": "B1", "name": "Silver bottle", "value": 8, "above_threshold": True},
                    ],
                },
            ],
        },
    }


def _study(study_type="grid"):
    return SimpleNamespace(
        id=uuid4(),
        study_type=study_type,
        title="Hand Wash",
        design_constraints=[],
        layers=[],
        classification_questions=[],
    )


class CompactCombinedTests(unittest.TestCase):
    def test_reads_combined_sheet(self):
        table = compact_combined(_combined_analysis(), "T")
        self.assertEqual(table["source"], "combined_sheet")
        self.assertEqual(table["base_size"], 40)
        codes = {el["code"] for el in table["elements"]}
        self.assertEqual(codes, {"A1", "A2", "B1"})
        self.assertIn("Gender::Male", [c["key"] for c in table["columns"]])
        self.assertIn("Age::18-24", [c["key"] for c in table["columns"]])
        class_keys = [c["key"] for c in table["columns"] if str(c["key"]).startswith("Classification::")]
        self.assertTrue(class_keys)
        self.assertEqual(table["sheet"], "(T) Combined")
        a1 = next(e for e in table["elements"] if e["code"] == "A1")
        self.assertEqual(a1["values"]["Overall"], 12)
        self.assertEqual(a1["values"]["Gender::Male"], 15)
        self.assertEqual(a1["values"]["Age::18-24"], 14)
        self.assertEqual(a1["values"]["Classification::How often::Daily"], 11)

    def test_prompt_json_keeps_gender_age_and_classification(self):
        from app.services.assistant_combined import combined_prompt_json
        import json

        blob = combined_prompt_json(_combined_analysis(), "T")
        table = json.loads(blob)
        self.assertEqual(table["sheet"], "(T) Combined")
        families = {c.get("family") or _family_guess(c["key"]) for c in table["columns"]}
        self.assertIn("Overall", families)
        self.assertIn("Gender", families)
        self.assertIn("Age", families)
        self.assertIn("Classification", families)
        self.assertNotIn("omitted", json.dumps(table).lower())

    def test_synthesizes_when_combined_sheet_missing(self):
        analysis = {
            "(T) Overall": _combined_analysis()["(T) Overall"],
            "(T) Gender": {
                "segments": {"Male": {"base_size": 22}},
                "categories": [
                    {
                        "code": "A",
                        "name": "Headline",
                        "elements": [
                            {
                                "code": "A1",
                                "name": "Kills 99.9% of germs",
                                "values": {"Male": 15},
                                "above_threshold": {"Male": True},
                            }
                        ],
                    }
                ],
            },
        }
        table = compact_combined(analysis, "T")
        self.assertEqual(table["source"], "synthesized")
        el = next(e for e in table["elements"] if e["code"] == "A1")
        self.assertEqual(el["values"]["Overall"], 12)
        self.assertEqual(el["values"]["Gender::Male"], 15)


def _family_guess(key: str) -> str:
    if key == "Overall":
        return "Overall"
    if str(key).startswith("Gender::"):
        return "Gender"
    if str(key).startswith("Age::"):
        return "Age"
    if str(key).startswith("Classification::"):
        return "Classification"
    return "Other"


class QueryCombinedTests(unittest.TestCase):
    def test_rank_overall_returns_verified_top_element(self):
        result = query_combined(
            _combined_analysis(),
            _study(),
            {"op": "rank", "column": "Overall", "limit": 1},
        )
        self.assertEqual(result["facts"][0]["code"], "A1")
        self.assertEqual(result["facts"][0]["value"], 12)
        self.assertEqual(result["blocks"][0]["type"], "top_bottom_elements")

    def test_rank_female_uses_that_column(self):
        result = query_combined(
            _combined_analysis(),
            _study(),
            {"op": "rank", "column": "women", "limit": 1},
        )
        self.assertEqual(result["facts"][0]["code"], "B1")
        self.assertEqual(result["facts"][0]["value"], 11)

    def test_rank_classification_answer_uses_that_combined_column(self):
        result = query_combined(
            _combined_analysis(),
            _study(),
            {"op": "rank", "column": "Daily", "limit": 1},
        )
        self.assertEqual(result["facts"][0]["code"], "A1")
        self.assertEqual(result["facts"][0]["value"], 11)
        self.assertIn("Daily", result["answer_text"])

    def test_best_segment_picks_strongest_column(self):
        result = query_combined(_combined_analysis(), _study(), {"op": "best_segment"})
        self.assertIn("Male", result["answer_text"])
        self.assertEqual(result["facts"][0]["value"], 15)
        self.assertEqual(result["blocks"][0]["type"], "segment_comparison")
        self.assertGreaterEqual(len(result["blocks"][0]["data"]["rows"]), 1)

    def test_unknown_element_does_not_invent_a_row(self):
        result = query_combined(
            _combined_analysis(),
            _study(),
            {"op": "lookup", "elements": ["purple unicorn"]},
        )
        self.assertIn("No Combined row matched", result["answer_text"])
        self.assertEqual(result["facts"], [])

    def test_compose_design_stacks_one_per_category(self):
        result = query_combined(
            _combined_analysis(),
            _study("layer"),
            {"op": "compose_design", "elements": ["A1", "B1"]},
        )
        self.assertEqual(result["blocks"][0]["type"], "top_k_designs")
        design = result["blocks"][0]["data"]["designs"][0]
        self.assertEqual(design["score"], 20)
        self.assertEqual(len(design["elements"]), 2)

    def test_planner_fallback_does_not_hardcode_the_question(self):
        plan = AssistantQueryPlan(
            tool=AssistantToolName.query_combined,
            metric=AssistantMetric.T,
            limit=2,
        )
        result = execute_combined_plan(
            _combined_analysis(),
            _study(),
            plan,
            message="what is the best segment overall in this study?",
        )
        self.assertIn("Male", result["answer_text"])


if __name__ == "__main__":
    unittest.main()
