"""Unit tests for assistant planner (deterministic path, no OpenAI required)."""

from __future__ import annotations

import unittest

from app.schemas.assistant_schema import (
    AssistantMetric,
    AssistantQueryPlan,
    AssistantQueryRequest,
    AssistantToolName,
    RankDirection,
)
from app.services.assistant_service import (
    _cache_key,
    _deterministic_plan,
    _extract_must_include,
    _has_responses,
    _is_simple_greeting,
    _normalize_plan_for_question,
    _parse_plan,
)


class AssistantPlannerTests(unittest.TestCase):
    def test_best_designs_plan(self):
        req = AssistantQueryRequest(message="give me top 10 best designs")
        plan = _deterministic_plan(req.message, req)
        self.assertEqual(plan.tool, AssistantToolName.rank_designs)
        self.assertEqual(plan.direction, RankDirection.highest)
        self.assertEqual(plan.limit, 10)

    def test_worst_designs_plan(self):
        req = AssistantQueryRequest(message="show least performing top 10 designs")
        plan = _deterministic_plan(req.message, req)
        self.assertEqual(plan.tool, AssistantToolName.rank_designs)
        self.assertEqual(plan.direction, RankDirection.lowest)

    def test_worst_performing_count_parses_before_designs(self):
        req = AssistantQueryRequest(message="worst performing 5 designs")
        plan = _normalize_plan_for_question(
            _deterministic_plan(req.message, req),
            req.message,
            "layer",
            req,
        )
        self.assertEqual(plan.tool, AssistantToolName.rank_designs)
        self.assertEqual(plan.direction, RankDirection.lowest)
        self.assertEqual(plan.limit, 5)

    def test_five_before_worst_parses_count(self):
        req = AssistantQueryRequest(message="give me 5 worst performing combinations")
        plan = _normalize_plan_for_question(
            _deterministic_plan(req.message, req),
            req.message,
            "layer",
            req,
        )
        self.assertEqual(plan.limit, 5)
        self.assertEqual(plan.direction, RankDirection.lowest)

    def test_top_two_designs_word_count(self):
        req = AssistantQueryRequest(message="top two designs")
        plan = _normalize_plan_for_question(
            _deterministic_plan(req.message, req),
            req.message,
            "layer",
            req,
        )
        self.assertEqual(plan.tool, AssistantToolName.rank_designs)
        self.assertEqual(plan.limit, 2)
        self.assertEqual(plan.direction, RankDirection.highest)

    def test_best_three_mixes_word_count(self):
        req = AssistantQueryRequest(message="show me the best three mixes")
        plan = _normalize_plan_for_question(
            _deterministic_plan(req.message, req),
            req.message,
            "layer",
            req,
        )
        self.assertEqual(plan.tool, AssistantToolName.rank_designs)
        self.assertEqual(plan.limit, 3)

    def test_winning_package_maps_to_rank_designs(self):
        req = AssistantQueryRequest(message="what is the winning package")
        plan = _normalize_plan_for_question(
            _deterministic_plan(req.message, req),
            req.message,
            "layer",
            req,
        )
        self.assertEqual(plan.tool, AssistantToolName.rank_designs)
        self.assertEqual(plan.limit, 1)

    def test_highest_scoring_combination_is_best_design_not_element(self):
        """Client phrasing must not treat 'highest-scoring' as a must_include element."""
        for msg in (
            "What is the highest-scoring combination?",
            "what is the highest scoring combination",
            "highest-scoring design",
            "show the top-performing combination",
        ):
            self.assertEqual(_extract_must_include(msg), [], msg)
            req = AssistantQueryRequest(message=msg)
            # Also strip bad must_include if the model wrongly filled it.
            dirty = AssistantQueryPlan(
                tool=AssistantToolName.rank_designs,
                must_include=["highest-scoring"],
                limit=4,
            )
            plan = _normalize_plan_for_question(dirty, msg, "layer", req)
            self.assertEqual(plan.tool, AssistantToolName.rank_designs, msg)
            self.assertEqual(plan.limit, 1, msg)
            self.assertEqual(plan.direction, RankDirection.highest, msg)
            self.assertEqual(plan.must_include, [], msg)

    def test_classification_plan(self):
        req = AssistantQueryRequest(message="how many answered this classification question and what are the other options")
        plan = _deterministic_plan(req.message, req)
        self.assertEqual(plan.tool, AssistantToolName.classification_distribution)

    def test_elements_with_segment(self):
        req = AssistantQueryRequest(message="which concepts perform best among women aged 25-34")
        plan = _deterministic_plan(req.message, req)
        self.assertEqual(plan.tool, AssistantToolName.rank_elements)
        self.assertEqual(plan.segment_key, "Female")
        # age also detected — gender takes precedence in current heuristic when both present;
        # ensure age is captured when gender absent
        req2 = AssistantQueryRequest(message="top elements for age 25-34")
        plan2 = _deterministic_plan(req2.message, req2)
        self.assertEqual(plan2.segment_key, "25-34")

    def test_model_gender_alias_is_canonicalized(self):
        req = AssistantQueryRequest(message="show best design for men")
        model_plan = _parse_plan(
            {
                "tool": "rank_designs",
                "metric": "T",
                "limit": 1,
                "segment_section": "gender",
                "segment_key": "men",
            },
            req,
        )
        plan = _normalize_plan_for_question(model_plan, req.message, "layer", req)
        self.assertEqual(plan.segment_section, "Gender")
        self.assertEqual(plan.segment_key, "Male")

    def test_greeting_uses_local_welcome_plan(self):
        req = AssistantQueryRequest(message="hello!")
        plan = _deterministic_plan(req.message, req)
        self.assertEqual(plan.tool, AssistantToolName.greeting)

    def test_parse_plan_rejects_unknown_tool(self):
        req = AssistantQueryRequest(message="hello")
        plan = _parse_plan({"tool": "hack_the_planet", "metric": "Z", "limit": 99}, req)
        self.assertEqual(plan.tool, AssistantToolName.clarify)
        self.assertEqual(plan.metric.value, "T")
        self.assertEqual(plan.limit, 20)

    def test_parse_plan_accepts_word_confidence(self):
        req = AssistantQueryRequest(message="show best design for men")
        plan = _parse_plan(
            {
                "tool": "rank_designs",
                "metric": "T",
                "limit": 1,
                "confidence": "high",
            },
            req,
        )
        self.assertEqual(plan.tool, AssistantToolName.rank_designs)
        self.assertEqual(plan.confidence, 0.9)

    def test_vague_best_overall_is_one_layer_design_using_t(self):
        req = AssistantQueryRequest(message="which is the best overall", metric=AssistantMetric.B)
        initial = _deterministic_plan(req.message, req)
        plan = _normalize_plan_for_question(initial, req.message, "layer", req)
        self.assertEqual(plan.tool, AssistantToolName.rank_designs)
        self.assertEqual(plan.limit, 1)
        self.assertEqual(plan.metric, AssistantMetric.T)
        self.assertIsNone(plan.segment_section)
        self.assertIsNone(plan.segment_key)

    def test_singular_and_plural_text_statement_semantics(self):
        singular_req = AssistantQueryRequest(message="which is the best statement overall")
        singular = _normalize_plan_for_question(
            _deterministic_plan(singular_req.message, singular_req),
            singular_req.message,
            "text",
            singular_req,
        )
        self.assertEqual(singular.tool, AssistantToolName.rank_elements)
        self.assertEqual(singular.limit, 1)

        plural_req = AssistantQueryRequest(message="show me the best statements overall")
        plural = _normalize_plan_for_question(
            _deterministic_plan(plural_req.message, plural_req),
            plural_req.message,
            "text",
            plural_req,
        )
        self.assertEqual(plural.tool, AssistantToolName.rank_elements)
        self.assertEqual(plural.limit, 4)

    def test_explicit_count_and_metric_are_respected(self):
        req = AssistantQueryRequest(message="show top 10 designs using bottom up")
        plan = _normalize_plan_for_question(
            _deterministic_plan(req.message, req),
            req.message,
            "grid",
            req,
        )
        self.assertEqual(plan.limit, 10)
        self.assertEqual(plan.metric, AssistantMetric.B)
        self.assertEqual(plan.direction, RankDirection.highest)

    def test_design_why_question_uses_explanation_tool(self):
        req = AssistantQueryRequest(message="why is this design better?")
        plan = _normalize_plan_for_question(
            _deterministic_plan(req.message, req),
            req.message,
            "layer",
            req,
        )
        self.assertEqual(plan.tool, AssistantToolName.explain_design)
        self.assertEqual(plan.limit, 2)

    def test_must_include_element_and_color_requests(self):
        specific = AssistantQueryRequest(
            message="Which is the top performing with A4-largecap-white-transp"
        )
        specific_plan = _normalize_plan_for_question(
            _deterministic_plan(specific.message, specific),
            specific.message,
            "layer",
            specific,
        )
        self.assertEqual(specific_plan.tool, AssistantToolName.rank_designs)
        self.assertEqual(specific_plan.limit, 1)
        self.assertTrue(
            any("A4-largecap-white-transp" in item for item in specific_plan.must_include)
        )

        color = AssistantQueryRequest(
            message="The top 2-3 performing combinations with a white colour"
        )
        color_plan = _normalize_plan_for_question(
            _deterministic_plan(color.message, color),
            color.message,
            "layer",
            color,
        )
        self.assertEqual(color_plan.tool, AssistantToolName.rank_designs)
        self.assertEqual(color_plan.limit, 3)
        self.assertTrue(any("white" in item.casefold() for item in color_plan.must_include))

    def test_must_include_which_has_keeps_specific_element_code(self):
        req = AssistantQueryRequest(
            message="Can you give me top 2 performing combination which has A6-silver-clinical"
        )
        plan = _normalize_plan_for_question(
            _deterministic_plan(req.message, req),
            req.message,
            "layer",
            req,
        )
        self.assertEqual(plan.tool, AssistantToolName.rank_designs)
        self.assertEqual(plan.limit, 2)
        self.assertIn("A6-silver-clinical", plan.must_include)
        # Must not collapse the code into a loose color token.
        self.assertFalse(any(item.casefold() == "silver" for item in plan.must_include))

    def test_must_include_multiple_named_ingredients(self):
        req = AssistantQueryRequest(
            message="top 2 combinations with holographic tick and silver-clinical"
        )
        plan = _normalize_plan_for_question(
            _deterministic_plan(req.message, req),
            req.message,
            "layer",
            req,
        )
        self.assertEqual(plan.tool, AssistantToolName.rank_designs)
        self.assertEqual(plan.limit, 2)
        self.assertTrue(any("holographic tick" in item.casefold() for item in plan.must_include))
        self.assertTrue(any("silver-clinical" in item.casefold() for item in plan.must_include))

    def test_single_age_snaps_to_bucket(self):
        for message, bucket in (
            ("which is best performing combination for the age of 45", "45-54"),
            ("best design for age 47", "45-54"),
            ("top designs for age group of 45-50", "45-54"),
            ("worst performing combination for age group of 45-54", "45-54"),
            ("best mix for age 22", "18-24"),
            ("top elements for age 65+", "65+"),
        ):
            req = AssistantQueryRequest(message=message)
            plan = _normalize_plan_for_question(
                _deterministic_plan(req.message, req),
                req.message,
                "layer",
                req,
            )
            self.assertEqual(plan.segment_section, "Age", msg=message)
            self.assertEqual(plan.segment_key, bucket, msg=message)

    def test_age_with_must_include_stays_rank_designs(self):
        req = AssistantQueryRequest(
            message="best performing for age 45 which contain white aerosol"
        )
        plan = _normalize_plan_for_question(
            _deterministic_plan(req.message, req),
            req.message,
            "layer",
            req,
        )
        self.assertEqual(plan.tool, AssistantToolName.rank_designs)
        self.assertEqual(plan.segment_section, "Age")
        self.assertEqual(plan.segment_key, "45-54")
        self.assertTrue(any("white" in item.casefold() for item in plan.must_include))

    def test_model_age_key_is_snapped(self):
        req = AssistantQueryRequest(message="best design for age 47")
        model_plan = _parse_plan(
            {
                "tool": "rank_designs",
                "metric": "T",
                "direction": "highest",
                "limit": 1,
                "segment_section": "Age",
                "segment_key": "47",
            },
            req,
        )
        plan = _normalize_plan_for_question(model_plan, req.message, "layer", req)
        self.assertEqual(plan.segment_key, "45-54")


class AgentRoutingTests(unittest.TestCase):
    """Guards on when the agent is skipped in favour of the cheaper planner."""

    def test_bare_pleasantries_skip_the_agent(self):
        for message in ("hi", "Hello!", "hey", "thanks", "Good morning", "thank you"):
            self.assertTrue(_is_simple_greeting(message), message)

    def test_real_questions_do_not_look_like_greetings(self):
        for message in (
            "hi, which element should I show the client?",
            "hello — compare men and women",
            "thanks, now show the worst design",
        ):
            self.assertFalse(_is_simple_greeting(message), message)

    def test_studies_without_responses_skip_the_agent(self):
        self.assertFalse(_has_responses(None))
        self.assertFalse(_has_responses({}))
        self.assertFalse(_has_responses({"dashboard_summary": {"totalResponses": 0}}))
        self.assertTrue(_has_responses({"dashboard_summary": {"totalResponses": 12}}))


class ResponseCacheKeyTests(unittest.TestCase):
    """
    Regression: the cached final answer must be scoped to the analysis settings
    active when it was produced. Without this, toggling something like
    with/without intercept keeps serving the old answer text (computed under the
    old setting) for up to ASSISTANT_CACHE_TTL_SECONDS, regardless of a page
    refresh or clearing the chat — those don't touch this server-side cache.
    """

    def _payload(self, analysis_settings):
        return {
            "assistant_semantics_version": 22,
            "message": "what is the top design combination",
            "filters": None,
            "metric": "T",
            "segment_section": None,
            "segment_key": None,
            "follow_up": None,
            "analysis_settings": analysis_settings,
        }

    def test_different_settings_produce_different_keys(self):
        key_without = _cache_key("study-1", "user-1", self._payload({"use_intercept": False}))
        key_with = _cache_key("study-1", "user-1", self._payload({"use_intercept": True}))
        self.assertNotEqual(key_without, key_with)

    def test_same_settings_reuse_the_same_key(self):
        key_a = _cache_key("study-1", "user-1", self._payload({"use_intercept": False}))
        key_b = _cache_key("study-1", "user-1", self._payload({"use_intercept": False}))
        self.assertEqual(key_a, key_b)


if __name__ == "__main__":
    unittest.main()
