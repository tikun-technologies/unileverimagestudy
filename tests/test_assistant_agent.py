"""Tests for the dynamic tool-calling analytics agent.

The OpenAI client is replaced by a scripted fake, so these tests exercise the
real loop, the real deterministic tools, and the real grounding validator
without any network calls.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from app.schemas.assistant_schema import (
    AssistantMetric,
    AssistantQueryRequest,
    AssistantToolName,
)
from app.services import assistant_agent
from app.services.assistant_agent import (
    AgentUnavailable,
    _grounding_values,
    _namespace_fact_ids,
    _numbers_are_grounded,
    _resolve_segment_ref,
    _tool_lookup_element_scores,
    _tool_segment_base_sizes,
    build_study_dictionary,
    run_agent_query,
)


def _analysis_fixture():
    """Two categories, gender + age sheets, mirroring the real report shape."""
    categories = [
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
                {"code": "B1", "name": "Silver clinical bottle", "value": 8, "above_threshold": True},
                {"code": "B2", "name": "Green natural bottle", "value": 1, "above_threshold": False},
            ],
        },
    ]

    def segment_categories(values_by_key):
        return [
            {
                "code": cat["code"],
                "name": cat["name"],
                "elements": [
                    {
                        "code": el["code"],
                        "name": el["name"],
                        "values": {key: values_by_key[key][el["code"]] for key in values_by_key},
                        "above_threshold": {key: True for key in values_by_key},
                    }
                    for el in cat["elements"]
                ],
            }
            for cat in categories
        ]

    gender_values = {
        "Male": {"A1": 15, "A2": -1, "B1": 4, "B2": 2},
        "Female": {"A1": 6, "A2": 5, "B1": 11, "B2": 0},
    }
    age_values = {
        "18-24": {"A1": 14, "A2": 0, "B1": 9, "B2": 1},
        "25-34": {"A1": 10, "A2": 2, "B1": 7, "B2": 3},
    }

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
        "Information Block": {"Study Title": "Demo", "Categories": []},
        "(T) Overall": {"base_size": 40, "threshold": 5, "categories": categories},
        "(T) Gender": {
            "threshold": 5,
            "segments": {"Male": {"base_size": 22}, "Female": {"base_size": 18}},
            "categories": segment_categories(gender_values),
        },
        "(T) Age": {
            "threshold": 5,
            "segments": {"18-24": {"base_size": 12}, "25-34": {"base_size": 28}},
            "categories": segment_categories(age_values),
        },
        "(T) Mindsets": {"threshold": 5, "segments": {}, "categories": []},
        "(T) Classification Questions": {"threshold": 5, "questions": [], "categories": []},
    }


def _study_fixture():
    return SimpleNamespace(
        id=uuid4(),
        study_type="grid",
        title="Hand Wash Claims",
        design_constraints=[],
        layers=[],
        classification_questions=[
            SimpleNamespace(
                id=uuid4(),
                question_text="How often do you buy hand wash?",
                answer_options=["Daily or almost daily", "A few times per month"],
                order=1,
            )
        ],
    )


# --------------------------------------------------------------------------- #
# Scripted OpenAI fake
# --------------------------------------------------------------------------- #

class _FakeToolCall:
    def __init__(self, name, arguments, call_id):
        self.id = call_id
        self.type = "function"
        self.function = SimpleNamespace(name=name, arguments=json.dumps(arguments))


class _FakeClient:
    """Replays a scripted list of turns: each is either tool calls or text."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._script:
            raise AssertionError("fake client ran out of scripted turns")
        turn = self._script.pop(0)
        if isinstance(turn, str):
            message = SimpleNamespace(content=turn, tool_calls=None)
        else:
            message = SimpleNamespace(
                content=None,
                tool_calls=[
                    _FakeToolCall(name, args, f"call_{i}")
                    for i, (name, args) in enumerate(turn)
                ],
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        )


def _run(script, question, *, analysis=None, study=None):
    client = _FakeClient(script)
    with patch.object(assistant_agent, "_openai_client", return_value=client):
        result = run_agent_query(
            None,
            study_obj=study or _study_fixture(),
            current_user=SimpleNamespace(id=uuid4(), email="a@b.com"),
            request=AssistantQueryRequest(message=question),
            analysis=analysis if analysis is not None else _analysis_fixture(),
            filters=None,
        )
    return result, client


# --------------------------------------------------------------------------- #

class StudyDictionaryTests(unittest.TestCase):
    def test_dictionary_lists_only_real_segments_and_elements(self):
        catalog = build_study_dictionary(_study_fixture(), _analysis_fixture())

        self.assertEqual(catalog["study_title"], "Hand Wash Claims")
        self.assertEqual(catalog["panelists"], 40)
        self.assertEqual(catalog["significance_threshold"], 5)
        self.assertIn("Male", catalog["segments"]["Gender"])
        self.assertIn("18-24", catalog["segments"]["Age"])
        self.assertEqual(catalog["segments"]["Mindsets"], [])

        headline = next(c for c in catalog["categories"] if c["name"] == "Headline")
        self.assertIn("A1: Kills 99.9% of germs", headline["elements"])

        question = catalog["classification_questions"][0]
        self.assertEqual(question["question"], "How often do you buy hand wash?")
        self.assertIn("Daily or almost daily", question["options"])

    def test_text_study_uses_statement_noun(self):
        study = _study_fixture()
        study.study_type = "text"
        self.assertEqual(build_study_dictionary(study, _analysis_fixture())["element_noun"], "statement")


class SegmentResolutionTests(unittest.TestCase):
    def test_colloquial_gender_resolves_to_canonical_key(self):
        self.assertEqual(_resolve_segment_ref("Gender", "men"), ("Gender", "Male"))
        self.assertEqual(_resolve_segment_ref(None, "women"), ("Gender", "Female"))

    def test_loose_age_snaps_to_bucket(self):
        self.assertEqual(_resolve_segment_ref("Age", "22 years old"), ("Age", "18-24"))

    def test_overall_clears_the_segment(self):
        self.assertEqual(_resolve_segment_ref("Overall", None), (None, None))
        self.assertEqual(_resolve_segment_ref(None, None), (None, None))

    def test_mindset_key_is_preserved(self):
        self.assertEqual(_resolve_segment_ref(None, "Mindset 2"), ("Mindsets", "Mindset 2"))


class PrimitiveToolTests(unittest.TestCase):
    def test_lookup_returns_exact_verified_value(self):
        result = _tool_lookup_element_scores(
            _analysis_fixture(), _study_fixture(), AssistantMetric.T, ["A1"], None, None
        )
        self.assertEqual(result["status"], "answered")
        self.assertEqual(len(result["facts"]), 1)
        self.assertEqual(result["facts"][0]["value"], 12)
        self.assertTrue(result["facts"][0]["above_threshold"])

    def test_lookup_is_segment_aware(self):
        female = _tool_lookup_element_scores(
            _analysis_fixture(), _study_fixture(), AssistantMetric.T, ["A1"], "Gender", "Female"
        )
        self.assertEqual(female["facts"][0]["value"], 6)

    def test_lookup_matches_on_name_not_only_code(self):
        result = _tool_lookup_element_scores(
            _analysis_fixture(), _study_fixture(), AssistantMetric.T, ["silver clinical"], None, None
        )
        self.assertEqual(result["facts"][0]["code"], "B1")

    def test_unknown_element_reports_what_exists(self):
        result = _tool_lookup_element_scores(
            _analysis_fixture(), _study_fixture(), AssistantMetric.T, ["purple unicorn"], None, None
        )
        self.assertEqual(result["status"], "not_found")
        self.assertIn("A1", result["answer_text"])

    def test_base_sizes_come_from_the_segment_sheet(self):
        result = _tool_segment_base_sizes(_analysis_fixture(), AssistantMetric.T, "Gender")
        values = {fact["label"]: fact["value"] for fact in result["facts"]}
        self.assertEqual(values["Base size — Male"], 22)
        self.assertEqual(values["Base size — Female"], 18)

    def test_missing_family_falls_back_to_overall(self):
        result = _tool_segment_base_sizes(_analysis_fixture(), AssistantMetric.T, "Mindsets")
        self.assertEqual(result["facts"][0]["value"], 40)


class GroundingTests(unittest.TestCase):
    def _allowed(self):
        return _grounding_values([{"facts": [{"value": 12}, {"value": 8}]}], "top 2 elements")

    def test_verbatim_numbers_pass(self):
        ok, offender = _numbers_are_grounded("A1 scores 12 and B1 scores 8.", self._allowed())
        self.assertTrue(ok, offender)

    def test_stated_gap_between_two_facts_passes(self):
        ok, offender = _numbers_are_grounded("A1 leads B1 by 4 points.", self._allowed())
        self.assertTrue(ok, offender)

    def test_invented_number_is_rejected(self):
        ok, offender = _numbers_are_grounded("A1 scores 47 points.", self._allowed())
        self.assertFalse(ok)
        self.assertEqual(offender, "47")

    def test_fact_citations_are_not_treated_as_numbers(self):
        ok, _ = _numbers_are_grounded("A1 wins [E1] over B1 [E2].", self._allowed())
        self.assertTrue(ok)

    def test_numbers_inside_labels_are_quotable_but_not_subtractable(self):
        """An element named "Kills 99.9% of germs" must not vouch for 99.9 - 13 = 86.9."""
        allowed = _grounding_values(
            [{"facts": [{"label": "Kills 99.9% of germs", "value": 12}]}], "best element?"
        )
        quoted, _ = _numbers_are_grounded("Lead with “Kills 99.9% of germs”, at 12.", allowed)
        self.assertTrue(quoted)
        derived, offender = _numbers_are_grounded("It scores 87.", allowed)
        self.assertFalse(derived)
        self.assertEqual(offender, "87")

    def test_tolerance_does_not_widen_with_magnitude(self):
        allowed = _grounding_values([{"facts": [{"value": 400}]}], "how many responses?")
        ok, offender = _numbers_are_grounded("There were 396 responses.", allowed)
        self.assertFalse(ok)
        self.assertEqual(offender, "396")


class FactNamespacingTests(unittest.TestCase):
    def test_ids_are_prefixed_so_parallel_calls_do_not_collide(self):
        payload = {
            "evidence": [{"fact_id": "E1", "label": "x"}],
            "blocks": [{"data": {"items": [{"fact_id": "E1"}]}}],
            "facts": [{"id": "L1"}],
        }
        out = _namespace_fact_ids(payload, "2.")
        self.assertEqual(out["evidence"][0]["fact_id"], "2.E1")
        self.assertEqual(out["blocks"][0]["data"]["items"][0]["fact_id"], "2.E1")
        self.assertEqual(out["facts"][0]["id"], "2.L1")

    def test_non_fact_ids_are_untouched(self):
        out = _namespace_fact_ids({"id": "some-uuid-value", "label": "E1"}, "2.")
        self.assertEqual(out["id"], "some-uuid-value")
        self.assertEqual(out["label"], "E1")


class AgentLoopTests(unittest.TestCase):
    def test_single_tool_answer_carries_blocks_and_evidence(self):
        result, client = _run(
            [
                [("rank_elements", {"metric": "T", "direction": "highest", "limit": 1})],
                [("respond", {
                    "answer": "Show them “Kills 99.9% of germs” — it scores 12 [E1], the highest of any claim.",
                    "follow_up_questions": ["Does that hold for women?"],
                    "data_backed": True,
                })],
            ],
            "which element should I show the client?",
        )

        self.assertEqual(result["status"], "answered")
        self.assertIn("Kills 99.9% of germs", result["answer_text"])
        self.assertEqual(result["agent_primary_tool"], AssistantToolName.rank_elements)
        self.assertTrue(result["agent_data_backed"])
        self.assertEqual(result["blocks"][0]["type"], "top_bottom_elements")
        self.assertFalse(result["usage"]["grounding_fallback"])
        self.assertEqual(result["usage"]["tool_calls"], 1)
        self.assertEqual(len(client.calls), 2)

    def test_multi_tool_composition_across_segments(self):
        """The question the old single-tool planner could not answer."""
        result, _ = _run(
            [
                [
                    ("rank_elements", {"segment_section": "Gender", "segment_key": "Male", "limit": 1}),
                    ("rank_elements", {"segment_section": "Gender", "segment_key": "Female", "limit": 1}),
                ],
                [("respond", {
                    "answer": "They split: men rank “Kills 99.9% of germs” first at 15 [E1], "
                              "women rank “Silver clinical bottle” first at 11 [2.E1].",
                    "data_backed": True,
                })],
            ],
            "what should I show men versus women?",
        )

        self.assertEqual(result["usage"]["tool_calls"], 2)
        self.assertIn("15", result["answer_text"])
        # Second call's facts are namespaced so citations stay unambiguous.
        self.assertTrue(any(f["fact_id"].startswith("2.") for f in result["evidence"]))

    def test_ungrounded_answer_falls_back_to_the_verified_template(self):
        result, _ = _run(
            [
                [("rank_elements", {"limit": 1})],
                [("respond", {"answer": "A1 scored 87 with 63% of respondents agreeing.", "data_backed": True})],
            ],
            "best element?",
        )

        self.assertTrue(result["usage"]["grounding_fallback"])
        self.assertNotIn("87", result["answer_text"])
        # The deterministic template answer took over.
        self.assertIn("Kills 99.9% of germs", result["answer_text"])

    def test_alternate_reading_becomes_the_first_follow_up(self):
        result, _ = _run(
            [
                [("rank_designs", {"limit": 1})],
                [("respond", {
                    "answer": "The winning mix pairs “Kills 99.9% of germs” with “Silver clinical bottle”.",
                    "alternate_reading": "Top elements instead of designs?",
                    "follow_up_questions": ["Why does it win?"],
                    "data_backed": True,
                })],
            ],
            "what's the best?",
        )

        self.assertEqual(result["follow_ups"][0], "Top elements instead of designs?")
        self.assertIn("Why does it win?", result["follow_ups"])

    def test_no_tool_answer_may_not_state_data(self):
        """A tool-free reply has nothing behind it, so a figure in it is a fabrication."""
        with self.assertRaises(AgentUnavailable):
            _run(["This study has 312 panelists and an average rating of 4.4."], "how many panelists?")

    def test_bare_lookup_never_substitutes_its_own_summary(self):
        """Falling back to '1 element score(s) for...' would be worse than replanning."""
        with self.assertRaises(AgentUnavailable):
            _run(
                [
                    [("lookup_element_scores", {"elements": ["A1"]})],
                    [("respond", {"answer": "A1 scored 87.", "data_backed": True})],
                ],
                "what does A1 score?",
            )

    def test_off_topic_answer_is_not_marked_verified(self):
        result, client = _run(
            ["I only cover this study's analytics. Ask me about the best design or the strongest claims."],
            "what's the weather in London?",
        )

        self.assertFalse(result["agent_data_backed"])
        self.assertEqual(result["usage"]["tool_calls"], 0)
        self.assertEqual(len(client.calls), 1)

    def test_tool_failure_does_not_sink_the_turn(self):
        result, _ = _run(
            [
                [("nonexistent_tool", {})],
                [("rank_elements", {"limit": 1})],
                [("respond", {"answer": "The top claim scores 12 [E1].", "data_backed": True})],
            ],
            "best element?",
        )

        self.assertIn("12", result["answer_text"])
        self.assertTrue(any(entry.get("error") for entry in result["usage"]["trace"]))

    def test_tool_budget_is_enforced(self):
        with patch.object(assistant_agent.settings, "ASSISTANT_AGENT_MAX_TOOL_CALLS", 1):
            result, _ = _run(
                [
                    [
                        ("rank_elements", {"limit": 1}),
                        ("rank_designs", {"limit": 1}),
                    ],
                    [("respond", {"answer": "The top claim scores 12 [E1].", "data_backed": True})],
                ],
                "best element and best design?",
            )
        self.assertEqual(result["usage"]["tool_calls"], 1)

    def test_last_round_forces_the_respond_tool(self):
        with patch.object(assistant_agent.settings, "ASSISTANT_AGENT_MAX_ROUNDS", 1):
            _, client = _run(
                [
                    [("rank_elements", {"limit": 1})],
                    [("respond", {"answer": "The top claim scores 12 [E1].", "data_backed": True})],
                ],
                "best element?",
            )
        self.assertEqual(client.calls[0]["tool_choice"], "auto")
        self.assertEqual(client.calls[-1]["tool_choice"]["function"]["name"], "respond")
        # Identical tool list every round keeps the cached prompt prefix intact.
        self.assertEqual(client.calls[0]["tools"], client.calls[-1]["tools"])

    def test_missing_client_defers_to_the_planner(self):
        with patch.object(assistant_agent, "_openai_client", return_value=None):
            with self.assertRaises(AgentUnavailable):
                run_agent_query(
                    None,
                    study_obj=_study_fixture(),
                    current_user=SimpleNamespace(id=uuid4(), email="a@b.com"),
                    request=AssistantQueryRequest(message="best element?"),
                    analysis=_analysis_fixture(),
                    filters=None,
                )

    def test_empty_answer_defers_to_the_planner(self):
        with self.assertRaises(AgentUnavailable):
            _run(
                [[("rank_elements", {"limit": 1})], [("respond", {"answer": "   "})]],
                "best element?",
            )


if __name__ == "__main__":
    unittest.main()
