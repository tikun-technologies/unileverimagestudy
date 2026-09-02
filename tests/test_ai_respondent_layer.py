"""Unit tests for synthetic AI layer rating (no OpenAI / network)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.synthetic.ai_respondent import AIRespondent


class _FakeComposer:
    def __init__(self, data_url=None):
        self.data_url = data_url
        self.calls = 0

    def compose_data_url(self, task, study_data):
        self.calls += 1
        return self.data_url


class _FakeChoice:
    def __init__(self, content):
        self.message = MagicMock(content=content)


class _FakeResponse:
    def __init__(self, rating=4, reasoning="looks good"):
        self.choices = [_FakeChoice('{"rating": %s, "reasoning": "%s"}' % (rating, reasoning))]


class AIRespondentLayerTests(unittest.TestCase):
    def _respondent(self):
        ai = AIRespondent(openai_api_key="test")
        ai.client = MagicMock()
        ai.client.chat.completions.create.return_value = _FakeResponse()
        return ai

    def test_layer_study_sends_one_composed_image(self):
        ai = self._respondent()
        composer = _FakeComposer("data:image/jpeg;base64,abc")
        task = {
            "elements_shown": {"Logo_1": 1, "Cap_1": 1},
            "elements_shown_content": {
                "Logo_1": {"url": "https://cdn.example.com/logo.png", "layer_name": "Logo"},
                "Cap_1": {"url": "https://cdn.example.com/cap.png", "layer_name": "Cap"},
            },
        }
        study = {
            "study_type": "layer",
            "background_image_url": "https://cdn.example.com/bg.png",
            "layer_layout": {
                "Logo": {"z_index": 2, "transform": {"x": 10, "y": 10, "width": 20, "height": 20}},
                "Cap": {"z_index": 1, "transform": {"x": 0, "y": 0, "width": 100, "height": 100}},
            },
            "_layer_composer": composer,
        }
        result = ai.rate_vignette_with_ai(task, "persona", study)
        self.assertEqual(result["method"], "ai")
        self.assertEqual(result["rating"], 4)
        self.assertEqual(composer.calls, 1)
        kwargs = ai.client.chat.completions.create.call_args.kwargs
        user_content = kwargs["messages"][1]["content"]
        image_parts = [part for part in user_content if part.get("type") == "image_url"]
        self.assertEqual(len(image_parts), 1)
        self.assertEqual(image_parts[0]["image_url"]["url"], "data:image/jpeg;base64,abc")
        self.assertIn("Composed design", user_content[0]["text"])
        self.assertIn("z-index=2", user_content[0]["text"])

    def test_layer_compose_failure_sends_background_and_layer_urls(self):
        ai = self._respondent()
        composer = _FakeComposer(None)
        task = {
            "elements_shown": {"Logo_1": 1},
            "elements_shown_content": {
                "Logo_1": {"url": "https://cdn.example.com/logo", "layer_name": "Logo"},
            },
        }
        study = {
            "study_type": "layer",
            "background_image_url": "https://cdn.example.com/bg.png",
            "layer_layout": {"Logo": {"z_index": 3, "transform": {"x": 1, "y": 2, "width": 3, "height": 4}}},
            "_layer_composer": composer,
        }
        ai.rate_vignette_with_ai(task, "persona", study)
        kwargs = ai.client.chat.completions.create.call_args.kwargs
        user_content = kwargs["messages"][1]["content"]
        urls = [part["image_url"]["url"] for part in user_content if part.get("type") == "image_url"]
        self.assertEqual(urls, ["https://cdn.example.com/bg.png", "https://cdn.example.com/logo"])
        self.assertIn("Composition of the stacked design failed", user_content[0]["text"])

    def test_composed_api_failure_retries_individual_urls(self):
        ai = self._respondent()
        composer = _FakeComposer("data:image/jpeg;base64,abc")
        ai.client.chat.completions.create.side_effect = [
            RuntimeError("payload too large"),
            _FakeResponse(rating=5, reasoning="retry ok"),
        ]
        task = {
            "elements_shown": {"Logo_1": 1},
            "elements_shown_content": {"Logo_1": {"url": "https://cdn.example.com/logo.png", "layer_name": "Logo"}},
        }
        study = {
            "study_type": "layer",
            "background_image_url": "https://cdn.example.com/bg.png",
            "_layer_composer": composer,
        }
        result = ai.rate_vignette_with_ai(task, "persona", study)
        self.assertEqual(result["rating"], 5)
        self.assertEqual(ai.client.chat.completions.create.call_count, 2)
        retry_content = ai.client.chat.completions.create.call_args_list[1].kwargs["messages"][1]["content"]
        urls = [part["image_url"]["url"] for part in retry_content if part.get("type") == "image_url"]
        self.assertIn("https://cdn.example.com/logo.png", urls)
        self.assertNotIn("data:image/jpeg;base64,abc", urls)

    def test_grid_study_still_sends_separate_images(self):
        ai = self._respondent()
        task = {
            "elements_shown": {"Cat_1": 1, "Cat_2": 1},
            "elements_shown_content": {
                "Cat_1": {"content": "https://cdn.example.com/a.png", "category_name": "A", "element_type": "image"},
                "Cat_2": {"content": "https://cdn.example.com/b.png", "category_name": "B", "element_type": "image"},
            },
        }
        result = ai.rate_vignette_with_ai(task, "persona", {"study_type": "grid"})
        self.assertEqual(result["method"], "ai")
        user_content = ai.client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        urls = [part["image_url"]["url"] for part in user_content if part.get("type") == "image_url"]
        self.assertEqual(urls, ["https://cdn.example.com/a.png", "https://cdn.example.com/b.png"])

    def test_randomize_skips_compose_and_api(self):
        ai = self._respondent()
        composer = _FakeComposer("data:image/jpeg;base64,abc")
        task = {
            "elements_shown": {"Logo_1": 1},
            "elements_shown_content": {"Logo_1": {"url": "https://cdn.example.com/logo.png"}},
        }
        result = ai.rate_vignette_with_ai(
            task,
            "persona",
            {"study_type": "layer", "randomize": True, "_layer_composer": composer},
        )
        self.assertEqual(result["method"], "fallback")
        self.assertEqual(composer.calls, 0)
        ai.client.chat.completions.create.assert_not_called()

    def test_empty_vignette_is_neutral(self):
        ai = self._respondent()
        result = ai.rate_vignette_with_ai(
            {"elements_shown": {}, "elements_shown_content": {}},
            "persona",
            {"study_type": "layer"},
        )
        self.assertEqual(result["rating"], 3)
        self.assertEqual(result["method"], "fallback")
        ai.client.chat.completions.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
