"""Tests for MindSurve analytics PowerPoint generation."""

from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

from app.schemas.assistant_schema import (
    AssistantMetric,
    AssistantQueryPlan,
    AssistantQueryRequest,
    AssistantToolName,
    AppliedContext,
)
from app.services.assistant_service import (
    _deterministic_plan,
    _is_ppt_generation_query,
    _normalize_plan_for_question,
)


def _fake_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (64, 64), (38, 116, 186, 255)).save(buf, format="PNG")
    return buf.getvalue()


class PptIntentTests(unittest.TestCase):
    def test_ppt_phrases_detected(self):
        phrases = [
            "generate a PPT for me",
            "create a powerpoint",
            "export presentation",
            "download slides for this study",
            "make a pptx deck",
            "build a presentation of the results",
            "Power Point please",
        ]
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertTrue(_is_ppt_generation_query(phrase.casefold()))

    def test_non_ppt_phrases_ignored(self):
        phrases = [
            "show top 5 designs",
            "executive summary",
            "compare male vs female",
            "what is the best element",
        ]
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertFalse(_is_ppt_generation_query(phrase.casefold()))

    def test_deterministic_plan_maps_to_generate_ppt(self):
        req = AssistantQueryRequest(message="generate a PPT for me")
        plan = _deterministic_plan(req.message, req)
        self.assertEqual(plan.tool, AssistantToolName.generate_ppt)

    def test_normalize_forces_generate_ppt(self):
        req = AssistantQueryRequest(message="please create a powerpoint presentation")
        plan = AssistantQueryPlan(tool=AssistantToolName.study_overview)
        normalized = _normalize_plan_for_question(plan, req.message, "grid", req)
        self.assertEqual(normalized.tool, AssistantToolName.generate_ppt)


class PptExportSmokeTests(unittest.TestCase):
    @patch("app.services.ppt_export.compose_design_preview", return_value=_fake_png())
    @patch("app.services.ppt_export.compose_element_thumb", return_value=_fake_png())
    @patch("app.services.ppt_export.tool_fatigue_summary", return_value={"answer_text": "No fatigue."})
    @patch("app.services.ppt_export.tool_response_time_summary", return_value={"answer_text": "Avg 2.1s."})
    @patch("app.services.ppt_export._biggest_age_element_gap", return_value=None)
    @patch("app.services.ppt_export._biggest_gender_element_gap", return_value=None)
    @patch("app.services.ppt_export._classification_rows", return_value=[])
    @patch("app.services.ppt_export.tool_use_avoid")
    @patch("app.services.ppt_export.tool_executive_summary")
    @patch("app.services.ppt_export.tool_rank_designs")
    @patch("app.services.ppt_export.tool_rank_elements")
    def test_build_analytics_pptx_smoke(
        self,
        mock_rank_elements,
        mock_rank_designs,
        mock_exec,
        mock_use_avoid,
        *_rest,
    ):
        from app.services.ppt_export import build_analytics_pptx
        from pptx import Presentation
        import io

        mock_rank_elements.return_value = {
            "blocks": [
                {
                    "type": "top_bottom_elements",
                    "data": {
                        "items": [
                            {
                                "rank": 1,
                                "name": "Blue pack",
                                "category": "Colour",
                                "value": 12.5,
                                "image_url": "https://example.com/blue.png",
                            },
                            {
                                "rank": 2,
                                "name": "Clean claim",
                                "category": "Claim",
                                "value": 9.1,
                                "image_url": "https://example.com/claim.png",
                            },
                        ]
                    },
                }
            ]
        }
        mock_rank_designs.return_value = {
            "blocks": [
                {
                    "type": "top_k_designs",
                    "data": {
                        "study_type": "layer",
                        "background_url": "https://example.com/bg.png",
                        "aspect_ratio": "9 / 16",
                        "designs": [
                            {
                                "rank": 1,
                                "score": 22.4,
                                "elements": [
                                    {
                                        "name": "Blue pack",
                                        "image_url": "https://example.com/blue.png",
                                        "z_index": 1,
                                        "transform": {"x": 0, "y": 0, "width": 100, "height": 100},
                                    },
                                    {
                                        "name": "Clean claim",
                                        "image_url": "https://example.com/claim.png",
                                        "z_index": 2,
                                        "transform": {"x": 10, "y": 10, "width": 80, "height": 80},
                                    },
                                ],
                            }
                        ],
                    },
                }
            ]
        }
        mock_exec.return_value = {
            "blocks": [
                {
                    "type": "executive_summary",
                    "data": {
                        "bullets": [
                            {"rank": 1, "title": "Top design", "text": "Best design scores 22.4."},
                        ]
                    },
                }
            ]
        }
        mock_use_avoid.return_value = {
            "blocks": [
                {
                    "type": "use_avoid",
                    "data": {
                        "use": [{"name": "Blue pack", "value": 12.5}],
                        "avoid": [{"name": "Busy label", "value": -3.2}],
                    },
                }
            ]
        }

        study = SimpleNamespace(title="Demo Pack Test", study_type="grid")
        analysis = {
            "dashboard_summary": {
                "uniquePanelists": 120,
                "totalResponses": 2400,
                "avgRating": 3.6,
                "avgResponseTime": 2.4,
                "ratingDistribution": [{"name": "4", "value": 40}],
            }
        }
        plan = AssistantQueryPlan(tool=AssistantToolName.generate_ppt, metric=AssistantMetric.T)
        context = AppliedContext(
            study_id="00000000-0000-0000-0000-000000000001",
            study_type="grid",
            study_title="Demo Pack Test",
            segment_label="Overall",
        )

        ppt_bytes, meta = build_analytics_pptx(
            db=MagicMock(),
            study_obj=study,
            analysis=analysis,
            plan=plan,
            context=context,
        )

        self.assertGreater(len(ppt_bytes), 2000)
        self.assertTrue(meta["filename"].endswith(".pptx"))
        self.assertGreaterEqual(meta["slide_count"], 15)

        prs = Presentation(io.BytesIO(ppt_bytes))
        self.assertGreaterEqual(len(prs.slides), 15)


if __name__ == "__main__":
    unittest.main()
