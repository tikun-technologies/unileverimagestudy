"""Retry-queue tests for synthetic AI ratings across study types."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.synthetic.ai_respondent import (
    AIRespondent,
    TransientAIRatingError,
    classify_ai_error,
    generate_panelist_response_from_json,
)


class _FakeChoice:
    def __init__(self, content):
        self.message = MagicMock(content=content)


class _FakeResponse:
    def __init__(self, rating=4, reasoning="looks good"):
        self.choices = [_FakeChoice('{"rating": %s, "reasoning": "%s"}' % (rating, reasoning))]


class _StatusError(Exception):
    def __init__(self, message, status_code=None, code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.body = body


def _rate_limit_exc():
    return _StatusError("Rate limit exceeded 429", status_code=429)


def _timeout_exc():
    return _StatusError("Request timed out", status_code=408)


def _server_exc():
    return _StatusError("Bad gateway", status_code=502)


def _bad_image_exc():
    return _StatusError(
        "Error while downloading file. Upstream status code: 400.",
        status_code=400,
        code="invalid_image_url",
        body={"error": {"code": "invalid_image_url", "message": "Error while downloading file."}},
    )


def _raise_rate_limit(*args, **kwargs):
    raise _rate_limit_exc()


def _raise_bad_image(*args, **kwargs):
    raise _bad_image_exc()


class ClassifyAIErrorTests(unittest.TestCase):
    def test_rate_limit_status_and_text(self):
        self.assertEqual(classify_ai_error(_StatusError("Rate limit exceeded 429", status_code=429)), "rate_limit")
        self.assertEqual(classify_ai_error(RuntimeError("Too Many Requests")), "rate_limit")

    def test_timeout_and_server_and_image(self):
        self.assertEqual(classify_ai_error(_StatusError("Request timed out", status_code=408)), "timeout")
        self.assertEqual(classify_ai_error(_StatusError("Bad gateway", status_code=502)), "server")
        self.assertEqual(
            classify_ai_error(_StatusError("Error while downloading file.", code="invalid_image_url")),
            "image_url",
        )
        self.assertEqual(classify_ai_error(RuntimeError("connection reset")), "connection")
        self.assertEqual(classify_ai_error(RuntimeError("payload too large")), "other")


class _RetryBase(unittest.TestCase):
    def _ai(self):
        ai = AIRespondent(openai_api_key="test")
        ai.client = MagicMock()
        return ai

    def _study(self, study_type, **extra):
        data = {
            "id": "study-1",
            "title": "Retry study",
            "study_type": study_type,
            "main_question": "How appealing?",
            "background": "Pack test",
            "objectives_text": "Rate the set",
            "_ai_retry_passes": 3,
            "_ai_retry_workers": (3, 2, 1),
            "_ai_retry_backoff": (0, 0, 0),
        }
        data.update(extra)
        return data

    def _panelist(self, number=1):
        return {
            "panelist_id": "p1",
            "panelist_number": number,
            "gender": "female",
            "age_range": "25 - 34",
            "answers": {
                "q1": {
                    "question_text": "Do you buy this?",
                    "answer": "Yes",
                    "answer_index": 0,
                    "order": 1,
                }
            },
        }

    def _grid_task(self, idx, urls, prefix="Cat"):
        shown = {}
        content = {}
        for i, url in enumerate(urls, start=1):
            key = f"{prefix}_{i}"
            shown[key] = 1
            content[key] = {
                "content": url,
                "category_name": prefix,
                "element_type": "image",
                "name": f"{prefix} {i}",
            }
        return {
            "task_id": f"1_{idx}",
            "task_index": idx,
            "elements_shown": shown,
            "elements_shown_content": content,
        }

    def _text_task(self, idx, claim):
        return {
            "task_id": f"1_{idx}",
            "task_index": idx,
            "elements_shown": {"Claim_1": 1},
            "elements_shown_content": {
                "Claim_1": {
                    "content": claim,
                    "category_name": "Claim",
                    "element_type": "text",
                    "name": claim,
                }
            },
        }

    def _layer_task(self, idx):
        return {
            "task_id": f"1_{idx}",
            "task_index": idx,
            "elements_shown": {"Logo_1": 1, "Cap_1": 1},
            "elements_shown_content": {
                "Logo_1": {"url": "https://cdn.example.com/logo.png", "layer_name": "Logo"},
                "Cap_1": {"url": "https://cdn.example.com/cap.png", "layer_name": "Cap"},
            },
        }

    def _run(self, ai, study, tasks, workers=10):
        def fake_init(self, openai_api_key=None, model=None):
            self.model = "gpt-4o-mini"
            self.client = ai.client

        with patch.object(AIRespondent, "__init__", fake_init):
            return generate_panelist_response_from_json(
                panelist_json=self._panelist(),
                tasks_json={"1": tasks},
                study_data=study,
                openai_api_key="test",
                max_vignette_workers=workers,
            )


class GridRetryQueueTests(_RetryBase):
    def test_rate_limit_parks_failed_task_and_finishes_others(self):
        ai = self._ai()
        fail_url = "https://cdn.example.com/fail.png"
        ok_a = "https://cdn.example.com/a.png"
        ok_b = "https://cdn.example.com/b.png"
        attempts = {fail_url: 0}

        def create(**kwargs):
            urls = [
                part["image_url"]["url"]
                for part in kwargs["messages"][1]["content"]
                if part.get("type") == "image_url"
            ]
            if fail_url in urls:
                attempts[fail_url] += 1
                if attempts[fail_url] == 1:
                    raise _rate_limit_exc()
                return _FakeResponse(rating=5, reasoning="dog after retry")
            if ok_a in urls:
                return _FakeResponse(rating=4, reasoning="cat ok")
            return _FakeResponse(rating=3, reasoning="car ok")

        ai.client.chat.completions.create.side_effect = create
        study = self._study("grid")
        tasks = [
            self._grid_task(0, [fail_url], "Fail"),
            self._grid_task(1, [ok_a], "OkA"),
            self._grid_task(2, [ok_b], "OkB"),
        ]
        result = self._run(ai, study, tasks, workers=3)
        ratings = result["task_ratings"]
        self.assertEqual([tr["method"] for tr in ratings], ["ai", "ai", "ai"])
        self.assertEqual(ratings[0]["rating"], 5)
        self.assertEqual(attempts[fail_url], 2)
        self.assertGreaterEqual(ai.client.chat.completions.create.call_count, 4)

    def test_three_retries_then_fallback(self):
        ai = self._ai()
        ai.client.chat.completions.create.side_effect = _raise_rate_limit
        study = self._study("grid")
        result = self._run(ai, study, [self._grid_task(0, ["https://cdn.example.com/a.png"])], workers=1)
        self.assertEqual(result["task_ratings"][0]["method"], "fallback")
        self.assertIn("3 retries", result["task_ratings"][0]["reasoning"])
        self.assertEqual(ai.client.chat.completions.create.call_count, 4)

    def test_timeout_and_5xx_are_retried(self):
        ai = self._ai()
        ai.client.chat.completions.create.side_effect = [
            _timeout_exc(),
            _server_exc(),
            _FakeResponse(rating=2, reasoning="recovered"),
        ]
        study = self._study("grid")
        result = self._run(ai, study, [self._grid_task(0, ["https://cdn.example.com/a.png"])], workers=1)
        self.assertEqual(result["task_ratings"][0]["method"], "ai")
        self.assertEqual(result["task_ratings"][0]["rating"], 2)
        self.assertEqual(ai.client.chat.completions.create.call_count, 3)

    def test_bad_image_skips_only_that_image(self):
        ai = self._ai()
        bad = "https://cdn.example.com/bad.png"
        good = "https://cdn.example.com/good.png"
        seen = []

        def create(**kwargs):
            urls = [
                part["image_url"]["url"]
                for part in kwargs["messages"][1]["content"]
                if part.get("type") == "image_url"
            ]
            seen.append(urls)
            if bad in urls:
                raise _bad_image_exc()
            return _FakeResponse(rating=4, reasoning="rated remaining photos including the good one")

        ai.client.chat.completions.create.side_effect = create
        study = self._study("grid")
        task = self._grid_task(0, [bad, good], "Mix")
        result = self._run(ai, study, [task], workers=1)
        self.assertEqual(result["task_ratings"][0]["method"], "ai")
        self.assertGreaterEqual(len(seen), 2)
        self.assertIn(bad, seen[0])
        self.assertIn(good, seen[0])
        self.assertNotIn(bad, seen[-1])
        self.assertIn(good, seen[-1])

    def test_backoff_sleeps_between_retry_passes(self):
        ai = self._ai()
        ai.client.chat.completions.create.side_effect = [
            _rate_limit_exc(),
            _rate_limit_exc(),
            _FakeResponse(rating=4, reasoning="after backoff"),
        ]
        study = self._study("grid")
        study["_ai_retry_backoff"] = (0.5, 1.0, 2.0)
        with patch("app.synthetic.ai_respondent.time.sleep") as sleep:
            result = self._run(
                ai,
                study,
                [self._grid_task(0, ["https://cdn.example.com/a.png"])],
                workers=1,
            )
        self.assertEqual(result["task_ratings"][0]["method"], "ai")
        sleep.assert_any_call(0.5)
        sleep.assert_any_call(1.0)

    def test_single_task_retries_three_times_before_success(self):
        ai = self._ai()
        ai.client.chat.completions.create.side_effect = [
            _rate_limit_exc(),
            _rate_limit_exc(),
            _rate_limit_exc(),
            _FakeResponse(rating=5, reasoning="ok on last retry"),
        ]
        study = self._study("grid")
        result = self._run(
            ai,
            study,
            [self._grid_task(0, ["https://cdn.example.com/a.png"])],
            workers=10,
        )
        self.assertEqual(result["task_ratings"][0]["method"], "ai")
        self.assertEqual(result["task_ratings"][0]["rating"], 5)
        self.assertEqual(ai.client.chat.completions.create.call_count, 4)

    def test_retry_worker_schedule_with_multiple_failures(self):
        ai = self._ai()
        worker_counts = []
        real_executor = __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor

        class RecordingExecutor(real_executor):
            def __init__(self, max_workers=None, **kwargs):
                worker_counts.append(max_workers)
                super().__init__(max_workers=max_workers, **kwargs)

        calls = {"n": 0}

        def create(**kwargs):
            calls["n"] += 1
            # First wave: fail all four. Later waves succeed.
            if calls["n"] <= 4:
                raise _rate_limit_exc()
            return _FakeResponse(rating=4, reasoning="retry ok")

        ai.client.chat.completions.create.side_effect = create
        study = self._study("grid")
        tasks = [self._grid_task(i, [f"https://cdn.example.com/{i}.png"], f"C{i}") for i in range(4)]
        with patch("app.synthetic.ai_respondent.ThreadPoolExecutor", RecordingExecutor):
            result = self._run(ai, study, tasks, workers=10)
        self.assertEqual([tr["method"] for tr in result["task_ratings"]], ["ai"] * 4)
        self.assertGreaterEqual(len(worker_counts), 2)
        self.assertEqual(worker_counts[0], 4)
        self.assertEqual(worker_counts[1], 3)


class TextAndHybridRetryTests(_RetryBase):
    def test_text_study_retries_then_succeeds(self):
        ai = self._ai()
        ai.client.chat.completions.create.side_effect = [
            _rate_limit_exc(),
            _FakeResponse(rating=3, reasoning="text claim after retry"),
        ]
        study = self._study("text")
        result = self._run(ai, study, [self._text_task(0, "Fresh for 24 hours")], workers=1)
        self.assertEqual(result["task_ratings"][0]["method"], "ai")
        self.assertEqual(result["task_ratings"][0]["rating"], 3)
        text = ai.client.chat.completions.create.call_args.kwargs["messages"][1]["content"][0]["text"]
        self.assertIn("Fresh for 24 hours", text)

    def test_hybrid_grid_and_text_tasks(self):
        ai = self._ai()

        def create(**kwargs):
            content = kwargs["messages"][1]["content"]
            text = content[0]["text"]
            if "https://cdn.example.com/grid.png" in str(content) or "[Image]" in text:
                return _FakeResponse(rating=5, reasoning="hybrid grid image")
            return _FakeResponse(rating=2, reasoning="hybrid text claim")

        ai.client.chat.completions.create.side_effect = create
        study = self._study("hybrid")
        tasks = [
            self._grid_task(0, ["https://cdn.example.com/grid.png"], "Pack"),
            self._text_task(1, "Soft on skin"),
        ]
        tasks[0]["phase_type"] = "grid"
        tasks[1]["phase_type"] = "text"
        result = self._run(ai, study, tasks, workers=2)
        self.assertEqual([tr["method"] for tr in result["task_ratings"]], ["ai", "ai"])
        self.assertEqual(result["task_ratings"][0]["rating"], 5)
        self.assertEqual(result["task_ratings"][1]["rating"], 2)


class LayerRetryTests(_RetryBase):
    def test_layer_429_is_parked_not_immediately_fallback(self):
        ai = self._ai()
        ai.client.chat.completions.create.side_effect = [
            _rate_limit_exc(),
            _FakeResponse(rating=5, reasoning="composed after retry"),
        ]
        study = self._study(
            "layer",
            background_image_url="https://cdn.example.com/bg.png",
            layer_layout={"Logo": {"z_index": 1, "transform": {"x": 0, "y": 0, "width": 100, "height": 100}}},
        )

        class _Composer:
            def compose_data_url(self, task, study_data):
                return "data:image/jpeg;base64,abc"

        study["_layer_composer"] = _Composer()
        result = self._run(ai, study, [self._layer_task(0)], workers=1)
        self.assertEqual(result["task_ratings"][0]["method"], "ai")
        self.assertEqual(result["task_ratings"][0]["rating"], 5)
        self.assertEqual(ai.client.chat.completions.create.call_count, 2)

    def test_layer_bad_source_url_skips_that_layer(self):
        ai = self._ai()
        seen = []

        def create(**kwargs):
            urls = [
                part["image_url"]["url"]
                for part in kwargs["messages"][1]["content"]
                if part.get("type") == "image_url"
            ]
            seen.append(urls)
            if "https://cdn.example.com/logo.png" in urls:
                raise _bad_image_exc()
            return _FakeResponse(rating=4, reasoning="rated remaining layer assets")

        ai.client.chat.completions.create.side_effect = create

        class _Composer:
            def compose_data_url(self, task, study_data):
                return None

        study = self._study(
            "layer",
            background_image_url="https://cdn.example.com/bg.png",
            _layer_composer=_Composer(),
        )
        result = self._run(ai, study, [self._layer_task(0)], workers=1)
        self.assertEqual(result["task_ratings"][0]["method"], "ai")
        self.assertTrue(any("https://cdn.example.com/logo.png" in urls for urls in seen))
        self.assertTrue(any("https://cdn.example.com/logo.png" not in urls and urls for urls in seen[1:]))


class DirectAttemptTests(_RetryBase):
    def test_attempt_raises_instead_of_fallback_on_429(self):
        ai = self._ai()
        ai.client.chat.completions.create.side_effect = _raise_rate_limit
        with self.assertRaises(TransientAIRatingError) as ctx:
            ai.rate_vignette_attempt(
                self._grid_task(0, ["https://cdn.example.com/a.png"]),
                "persona",
                self._study("grid"),
            )
        self.assertEqual(ctx.exception.kind, "rate_limit")

    def test_direct_rate_vignette_still_fallbacks(self):
        ai = self._ai()
        ai.client.chat.completions.create.side_effect = _raise_rate_limit
        result = ai.rate_vignette_with_ai(
            self._grid_task(0, ["https://cdn.example.com/a.png"]),
            "persona",
            self._study("grid"),
        )
        self.assertEqual(result["method"], "fallback")

    def test_randomize_never_calls_api(self):
        ai = self._ai()
        result = self._run(
            ai,
            self._study("grid", randomize=True),
            [self._grid_task(0, ["https://cdn.example.com/a.png"])],
            workers=2,
        )
        self.assertEqual(result["task_ratings"][0]["method"], "fallback")
        ai.client.chat.completions.create.assert_not_called()

    def test_empty_task_does_not_retry(self):
        ai = self._ai()
        empty = {"task_id": "1_0", "task_index": 0, "elements_shown": {}, "elements_shown_content": {}}
        result = self._run(ai, self._study("grid"), [empty], workers=1)
        self.assertEqual(result["task_ratings"][0]["method"], "fallback")
        self.assertEqual(result["task_ratings"][0]["rating"], 3)
        ai.client.chat.completions.create.assert_not_called()

    def test_special_creator_keeps_polar_ai_rating(self):
        ai = self._ai()
        ai.client.chat.completions.create.return_value = _FakeResponse(rating=4, reasoning="ok")
        result = self._run(
            ai,
            self._study("text", is_special_creator=True),
            [self._text_task(0, "Bold claim")],
            workers=1,
        )
        self.assertEqual(result["task_ratings"][0]["rating"], 5)
        self.assertEqual(result["task_ratings"][0]["method"], "ai")


if __name__ == "__main__":
    unittest.main()
