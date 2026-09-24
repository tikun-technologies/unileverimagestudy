"""
AI Respondent for generating survey responses based on panelist personas.

This module provides functionality to generate survey responses using AI,
where each response rates entire vignettes (combinations of elements) rather
than individual elements.
"""

import json
import logging
import os
import random
import time
from typing import Dict, List, Any, Optional, Set, Tuple
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.synthetic.layer_stimulus import (
    StimulusComposer,
    describe_layer_stack,
    enrich_shown_element,
    is_layer_study,
    is_probably_image_url,
    iter_shown_elements,
)

logger = logging.getLogger(__name__)

# First pass uses the caller's worker count. Failed tasks are parked, then
# retried 3 times at lower concurrency so a 429 does not become a random rating.
AI_RETRY_PASSES = 3
AI_RETRY_WORKERS = (3, 2, 1)
AI_RETRY_BACKOFF_SECONDS = (1.0, 2.0, 4.0)

# Try to load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Try to import OpenAI, but make it optional
try:
    from openai import OpenAI  # type: ignore
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


class TransientAIRatingError(Exception):
    """A vignette rating failed but should be retried later, not randomized yet."""

    def __init__(
        self,
        message: str,
        kind: str = "other",
        exclude_urls: Optional[Set[str]] = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.exclude_urls = set(exclude_urls or [])


def classify_ai_error(exc: BaseException) -> str:
    """Classify an OpenAI / network failure so the queue can retry the right way."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None

    code = None
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        code = err.get("code")
    code = str(code or getattr(exc, "code", "") or "").strip().lower()
    text = str(exc).lower()

    if (
        status_int == 429
        or code in {"rate_limit", "rate_limit_exceeded", "rate_limit_error"}
        or "rate_limit" in text
        or "rate limit" in text
        or "too many requests" in text
        or "429" in text
    ):
        return "rate_limit"
    if (
        code == "invalid_image_url"
        or "invalid_image_url" in text
        or "error while downloading file" in text
        or "invalid image" in text
    ):
        return "image_url"
    if (
        status_int == 408
        or "timeout" in text
        or "timed out" in text
        or "deadline exceeded" in text
    ):
        return "timeout"
    if status_int is not None and status_int >= 500:
        return "server"
    if "connection" in text or "connect" in text:
        return "connection"
    return "other"


def _image_url_from_part(part: Any) -> Optional[str]:
    if not isinstance(part, dict) or part.get("type") != "image_url":
        return None
    image = part.get("image_url")
    if isinstance(image, dict):
        url = image.get("url")
        return url if isinstance(url, str) and url else None
    if isinstance(image, str) and image:
        return image
    return None


def _next_image_to_drop(image_urls: List[str], exclude_urls: Set[str], exc: BaseException) -> Optional[str]:
    remaining = [url for url in image_urls if url and url not in exclude_urls]
    if not remaining:
        return None
    text = str(exc)
    for url in remaining:
        if url and url in text:
            return url
    return remaining[0]


class AIRespondent:
    """
    AI-powered respondent that simulates survey responses based on a panelist persona.
    
    This class handles rating entire vignettes (combinations of elements) based on
    the panelist's classification answers and persona.
    """
    
    def __init__(self, openai_api_key: Optional[str] = None, model: Optional[str] = None):
        """
        Initialize the AI Respondent.
        
        Args:
            openai_api_key: OpenAI API key (or set OPENAI_API_KEY env var)
            model: OpenAI model to use (default: gpt-4o-mini or OPENAI_MODEL env var)
                   Note: For image support, use vision-capable models like gpt-4o or gpt-4o-mini
        """
        self.model = model or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
        self.client = None
        
        if OPENAI_AVAILABLE:
            api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
            if api_key:
                self.client = OpenAI(api_key=api_key)
            else:
                print("Warning: No OpenAI API key provided. AI features will be disabled.")
                print("Set OPENAI_API_KEY environment variable or pass api_key parameter.")
        else:
            print("Warning: OpenAI library not available. Install with: pip install openai")
            print("AI features will use fallback heuristic ratings.")
    
    def build_persona_prompt(self, panelist: Dict[str, Any], study_data: Dict[str, Any]) -> str:
        """
        Build a detailed persona prompt for the AI based on panelist answers and demographics.
        
        Args:
            panelist: Panelist dictionary with classification answers, gender, and age_range
            study_data: Study data with background, objectives, etc.
        
        Returns:
            Formatted persona prompt string
        """
        answers = panelist.get('answers', {})
        gender = panelist.get('gender', 'unknown')
        age_range = panelist.get('age_range', 'unknown')
        
        # Build factors from preliminary questions (classification answers)
        factors_lines = []
        for q_id in sorted(answers.keys(), key=lambda x: answers[x].get('order', 0)):
            answer_data = answers[q_id]
            factors_lines.append(f"- {answer_data['question_text']}: {answer_data['answer']}")
        
        factors_text = "\n".join(factors_lines) if factors_lines else "- No preliminary factors defined"
        
        # Get study background and objectives
        background = study_data.get('background', '')
        objectives = study_data.get('objectives_text', study_data.get('orientation_text', ''))
        main_question = study_data.get('main_question', '')
        
        # Get rating scale labels
        rating_scale = study_data.get('rating_scale', {})
        min_label = rating_scale.get('min_label', 'Bad (does not appeal to you, doesn\'t align with your preferences)')
        middle_label = rating_scale.get('middle_label', 'Bad (does not appeal to you, doesn\'t align with your preferences)')
        max_label = rating_scale.get('max_label', 'Very good (strongly appeals to you, perfectly aligns with your preferences)')
        
        prompt = f"""You are a role-based evaluator. Your judgment should reflect how things make sense from within the role defined. Do not generalize beyond it. You are evaluating from a specific human context defined on the following role assigned to you.

You are taking on the following role. You are  representing a lived context shaped by the following factors

{factors_text}

These factors influence your sensitivity, priorities, and interpretation, NOT expertise or factual knowledge.

Do NOT evaluate scientific accuracy, technical feasibility, or clinical truth. Evaluate perceived sense-making only specific to your current role defined by the factors above.

You are currently participating in an evaluation where this role evaluates various ideas related to the following background and objectives.

Background: {background}

Objectives: {objectives}

Now you will be presented with a set of stimuli. There are multiple elements together to present ONE stimulus object. Based on your current role evaluate whole set as a combined proposition to elicit a rating response the following question

{main_question}

Rate the entire SET as a WHOLE in response to the question on a scale of 1-5 where:

<<1 = {min_label}
2 = 
3 = {middle_label}
4 = 
5 = {max_label}>>"""
        
        return prompt

    def _build_rating_payload(
        self,
        task: Dict[str, Any],
        persona_prompt: str,
        study_context: Dict[str, Any],
        exclude_urls: Optional[Set[str]] = None,
        *,
        force_layer_urls: bool = False,
    ) -> Dict[str, Any]:
        exclude_urls = set(exclude_urls or [])
        shown_pairs = iter_shown_elements(task)
        layer_mode = is_layer_study(study_context)
        vignette_text_parts: List[str] = []
        image_parts: List[Dict[str, Any]] = []
        composed = False

        if layer_mode and not force_layer_urls:
            composer = study_context.get("_layer_composer")
            if composer is None or not callable(getattr(composer, "compose_data_url", None)):
                composer = StimulusComposer()
                if isinstance(study_context, dict):
                    study_context["_layer_composer"] = composer
            composed_url = composer.compose_data_url(task, study_context)
            vignette_text_parts.append(describe_layer_stack(task, study_context))
            if composed_url and composed_url not in exclude_urls:
                image_parts.append({
                    "type": "image_url",
                    "image_url": {"url": composed_url},
                })
                composed = True
            else:
                image_parts.extend(self._layer_fallback_image_parts(shown_pairs, study_context, exclude_urls))
        elif layer_mode:
            vignette_text_parts.append(describe_layer_stack(task, study_context))
            image_parts.extend(self._layer_fallback_image_parts(shown_pairs, study_context, exclude_urls))
        else:
            for key, element in shown_pairs:
                category = element.get("category_name") or element.get("layer_name") or "Unknown"
                content = element.get("content") or element.get("url") or element.get("name", "Unknown")
                element_type = element.get("element_type", "text")
                url = content if isinstance(content, str) else None
                if is_probably_image_url(url, element_type, layer_mode=False):
                    if url in exclude_urls:
                        vignette_text_parts.append(f"{category}: [Image omitted — unavailable]")
                    else:
                        image_parts.append({
                            "type": "image_url",
                            "image_url": {"url": url},
                        })
                        vignette_text_parts.append(f"{category}: [Image]")
                else:
                    vignette_text_parts.append(f"{category}: {content}")

        vignette_text = "\n".join(vignette_text_parts)
        has_images = len(image_parts) > 0
        if composed:
            image_instruction = """
IMPORTANT: The attached image is the EXACT composed stimulus shown to human participants:
background image plus every visible layer stacked by z-index, each placed/sized by its transform.
Rate this one assembled design as a whole. Do not treat layers as separate products.
"""
        elif layer_mode and has_images:
            image_instruction = """
IMPORTANT: Composition of the stacked design failed, so you are seeing separate layer assets.
Mentally assemble them using the listed z-index (higher = in front) and transform percents
(x/y/width/height of the background box). Include the background if one is listed.
Rate the assembled design, not the loose assets.
"""
        elif has_images:
            image_instruction = """
IMPORTANT: This stimulus set contains IMAGES. Please carefully analyze each image visually:
- Look at the visual design, colors, composition, and overall aesthetic
- Consider how the images relate to the background and objectives
- Evaluate how well the images resonate with your role and the factors shaping your perspective
- Assess how the images work together as a cohesive visual experience
- Rate based on perceived sense-making from your role, NOT technical accuracy
"""
        else:
            image_instruction = ""

        text_prompt = f"""{persona_prompt}

STIMULUS SET (evaluate as ONE combined proposition):
{vignette_text}
{image_instruction}

Respond ONLY with a JSON object in this exact format:
{{
    "rating": <number between 1 and 5>,
    "reasoning": "<brief explanation of why you gave this rating based on your role, the factors shaping your perspective, and how the stimulus set resonates with you>"
}}
"""
        user_content: List[Any] = [{"type": "text", "text": text_prompt}]
        user_content.extend(image_parts)
        image_urls = [url for url in (_image_url_from_part(part) for part in image_parts) if url]
        return {
            "user_content": user_content,
            "image_urls": image_urls,
            "composed": composed,
            "layer_mode": layer_mode,
            "shown_pairs": shown_pairs,
            "vignette_text": vignette_text,
        }

    def rate_vignette_attempt(
        self,
        task: Dict[str, Any],
        persona_prompt: str,
        study_context: Dict[str, Any],
        exclude_urls: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """
        One AI rating attempt. Transient failures raise TransientAIRatingError
        so the caller can park the task and finish other work first.
        """
        exclude_urls = set(exclude_urls or [])
        if study_context.get("randomize") or not self.client:
            return self._generate_fallback_vignette_rating(task, persona_prompt, study_context)

        shown_pairs = iter_shown_elements(task)
        if not shown_pairs:
            no_rating = 5 if study_context.get("is_special_creator") else 3
            return {
                "rating": no_rating,
                "reasoning": "No elements shown in this vignette",
                "method": "fallback",
            }

        payload = self._build_rating_payload(task, persona_prompt, study_context, exclude_urls)
        try:
            return self._complete_vignette_rating(payload["user_content"], study_context)
        except Exception as exc:
            kind = classify_ai_error(exc)
            current_exclude = set(exclude_urls)

            # Composed JPEG failed for a payload/image reason: try source layer URLs now.
            # Rate limits / timeouts / 5xx are parked instead so we do not double-hit the API.
            if payload["composed"] and kind not in {"rate_limit", "timeout", "server"}:
                logger.info("Composed layer image failed (%s): %s. Trying individual layer URLs.", kind, exc)
                try:
                    layer_payload = self._build_rating_payload(
                        task,
                        persona_prompt,
                        study_context,
                        current_exclude,
                        force_layer_urls=True,
                    )
                    return self._complete_vignette_rating(layer_payload["user_content"], study_context)
                except Exception as layer_exc:
                    exc = layer_exc
                    kind = classify_ai_error(layer_exc)
                    payload = layer_payload

            if kind == "image_url":
                drop = _next_image_to_drop(payload["image_urls"], current_exclude, exc)
                if drop:
                    current_exclude.add(drop)
                    logger.info("Skipping unavailable image and retrying the remaining set: %s", drop[:160])
                    try:
                        skipped_payload = self._build_rating_payload(
                            task,
                            persona_prompt,
                            study_context,
                            current_exclude,
                            force_layer_urls=payload["layer_mode"] and not payload["composed"],
                        )
                        if payload["layer_mode"] and payload["composed"]:
                            skipped_payload = self._build_rating_payload(
                                task,
                                persona_prompt,
                                study_context,
                                current_exclude,
                                force_layer_urls=True,
                            )
                        result = self._complete_vignette_rating(skipped_payload["user_content"], study_context)
                        result["excluded_image_urls"] = sorted(current_exclude)
                        return result
                    except Exception as skip_exc:
                        exc = skip_exc
                        kind = classify_ai_error(skip_exc)

            raise TransientAIRatingError(str(exc), kind=kind, exclude_urls=current_exclude) from exc

    def rate_vignette_with_ai(self, task: Dict[str, Any], persona_prompt: str, study_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Rate an entire vignette (task with combined elements) based on the persona.
        
        IMPORTANT: This rates the ENTIRE VIGNETTE as a whole, not individual elements.
        A vignette is a combination of multiple elements shown together, and we rate
        how well this combination works together based on the persona.
        
        Args:
            task: Task dictionary with elements_shown_content and elements_shown
            persona_prompt: The persona description
            study_context: Study context (background, main question, etc.)
        
        Returns:
            Dictionary with rating (1-5) and reasoning for the entire vignette
        """
        try:
            return self.rate_vignette_attempt(task, persona_prompt, study_context)
        except TransientAIRatingError as exc:
            logger.warning("AI rating failed after local recovery (%s): %s. Using fallback.", exc.kind, exc)
            return self._generate_fallback_vignette_rating(task, persona_prompt, study_context)

    def _layer_fallback_image_parts(
        self,
        shown_pairs: List[Any],
        study_context: Dict[str, Any],
        exclude_urls: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        exclude_urls = set(exclude_urls or [])
        parts: List[Dict[str, Any]] = []
        seen = set()
        bg = ""
        if isinstance(study_context, dict):
            bg = str(study_context.get("background_image_url") or "").strip()
        if bg and is_probably_image_url(bg, "image", layer_mode=True) and bg not in seen and bg not in exclude_urls:
            seen.add(bg)
            parts.append({"type": "image_url", "image_url": {"url": bg}})
        for key, element in shown_pairs:
            enriched = enrich_shown_element(key, element, study_context)
            url = enriched.get("url")
            if (
                url
                and url not in seen
                and url not in exclude_urls
                and is_probably_image_url(url, enriched.get("element_type"), layer_mode=True)
            ):
                seen.add(url)
                parts.append({"type": "image_url", "image_url": {"url": url}})
        return parts

    def _complete_vignette_rating(self, user_content: List[Any], study_context: Dict[str, Any]) -> Dict[str, Any]:
        if not self.client:
            raise RuntimeError("OpenAI client is not available")
        semaphore = study_context.get("_ai_semaphore") if isinstance(study_context, dict) else None
        if semaphore is not None:
            semaphore.acquire()
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a role-based evaluator providing ratings from within a defined human context. Your judgment reflects perceived sense-making, not expertise or factual knowledge. Always respond with valid JSON only."},
                    {"role": "user", "content": user_content}
                ],
                temperature=0.7,
                response_format={"type": "json_object"}
            )
        finally:
            if semaphore is not None:
                try:
                    semaphore.release()
                except Exception:
                    pass
        raw_content = response.choices[0].message.content if response.choices else None
        result = json.loads(raw_content or "{}")
        try:
            rating = int(round(float(result.get("rating", 3))))
        except (TypeError, ValueError):
            rating = 3
        rating = max(1, min(5, rating))
        if study_context.get("is_special_creator"):
            rating = 1 if rating < 3 else 5
        return {
            "rating": rating,
            "reasoning": result.get("reasoning", ""),
            "method": "ai",
        }
    
    def _generate_fallback_vignette_rating(
        self, task: Dict[str, Any], persona_prompt: str, study_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Generate a fallback rating for a vignette when AI is not available.
        
        Args:
            task: Task dictionary
            persona_prompt: Persona description (not used in fallback, but kept for consistency)
            study_context: Optional study context; if is_special_creator is True, rating is 1 or 5 only.
        
        Returns:
            Dictionary with rating and reasoning
        """
        if study_context and study_context.get("is_special_creator"):
            rating = random.choice([1, 5])
        else:
            # Base rating around 3 (neutral)
            rating = 3
            rating += random.randint(-1, 1)
            rating = max(1, min(5, rating))
        
        return {
            'rating': rating,
            'reasoning': 'Fallback heuristic rating (AI not available)',
            'method': 'fallback'
        }


def _retry_settings(study_data: Dict[str, Any]) -> Tuple[int, Tuple[int, ...], Tuple[float, ...]]:
    passes = int(study_data.get("_ai_retry_passes", AI_RETRY_PASSES) or AI_RETRY_PASSES)
    workers = study_data.get("_ai_retry_workers", AI_RETRY_WORKERS) or AI_RETRY_WORKERS
    backoff = study_data.get("_ai_retry_backoff", AI_RETRY_BACKOFF_SECONDS) or AI_RETRY_BACKOFF_SECONDS
    workers_tuple = tuple(max(1, int(w)) for w in workers)
    backoff_tuple = tuple(max(0.0, float(s)) for s in backoff)
    return max(0, passes), workers_tuple, backoff_tuple


def _task_result_shell(
    panelist_number: Any,
    idx: int,
    task: Dict[str, Any],
    study_data: Dict[str, Any],
    rating_result: Dict[str, Any],
) -> Dict[str, Any]:
    task_id = task.get("task_id", f"{panelist_number}_{idx}")
    task_index = task.get("task_index", idx)
    shown_elements = []
    vignette_parts = []
    for key, element_data in iter_shown_elements(task):
        enriched = enrich_shown_element(key, element_data, study_data)
        url_or_content = enriched.get("content") or enriched.get("url") or enriched.get("name")
        shown_elements.append({
            "key": key,
            "element_id": enriched.get("element_id") or element_data.get("element_id"),
            "name": enriched.get("name"),
            "content": url_or_content,
            "category_name": enriched.get("category_name") or enriched.get("layer_name"),
            "element_type": enriched.get("element_type") or "text",
        })
        vignette_parts.append(f"{enriched.get('category_name') or 'Unknown'}: {url_or_content}")
    return {
        "task_id": task_id,
        "task_index": task_index,
        "main_question": study_data.get("main_question", ""),
        "vignette_content": "\n".join(vignette_parts),
        "rating": rating_result["rating"],
        "reasoning": rating_result.get("reasoning", ""),
        "elements_shown": shown_elements,
        "method": rating_result.get("method", "unknown"),
        "_original_index": idx,
    }


def _run_rating_pass(
    ai_respondent: AIRespondent,
    pending: List[Dict[str, Any]],
    persona_prompt: str,
    study_data: Dict[str, Any],
    panelist_number: Any,
    max_workers: int,
) -> Tuple[Dict[int, Dict[str, Any]], List[Dict[str, Any]]]:
    if not pending:
        return {}, []
    workers = max(1, min(int(max_workers or 1), len(pending)))
    completed: Dict[int, Dict[str, Any]] = {}
    failed: List[Dict[str, Any]] = []

    def _one(item: Dict[str, Any]) -> Dict[str, Any]:
        rating_result = ai_respondent.rate_vignette_attempt(
            item["task"],
            persona_prompt,
            study_data,
            item.get("exclude_urls") or set(),
        )
        return _task_result_shell(panelist_number, item["idx"], item["task"], study_data, rating_result)

    if workers == 1:
        for item in pending:
            try:
                result = _one(item)
                completed[item["idx"]] = result
            except TransientAIRatingError as exc:
                item["exclude_urls"] = set(exc.exclude_urls)
                item["last_error"] = str(exc)
                item["last_kind"] = exc.kind
                failed.append(item)
                logger.warning(
                    "Parking vignette %s after %s: %s",
                    item.get("idx"),
                    exc.kind,
                    exc,
                )
            except Exception as exc:
                item["exclude_urls"] = set(item.get("exclude_urls") or set())
                item["last_error"] = str(exc)
                item["last_kind"] = classify_ai_error(exc)
                failed.append(item)
                logger.warning("Parking vignette %s after unexpected error: %s", item.get("idx"), exc)
        return completed, failed

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_item = {executor.submit(_one, item): item for item in pending}
        for future in as_completed(future_to_item):
            item = future_to_item[future]
            try:
                result = future.result()
                completed[item["idx"]] = result
            except TransientAIRatingError as exc:
                item["exclude_urls"] = set(exc.exclude_urls)
                item["last_error"] = str(exc)
                item["last_kind"] = exc.kind
                failed.append(item)
                logger.warning(
                    "Parking vignette %s after %s: %s",
                    item.get("idx"),
                    exc.kind,
                    exc,
                )
            except Exception as exc:
                item["exclude_urls"] = set(item.get("exclude_urls") or set())
                item["last_error"] = str(exc)
                item["last_kind"] = classify_ai_error(exc)
                failed.append(item)
                logger.warning("Parking vignette %s after unexpected error: %s", item.get("idx"), exc)
    return completed, failed


def generate_panelist_response_from_json(
    panelist_json: Dict[str, Any],
    tasks_json: Dict[str, List[Dict[str, Any]]],
    study_data: Dict[str, Any],
    openai_api_key: Optional[str] = None,
    model: Optional[str] = None,
    max_vignette_workers: int = 10
) -> Dict[str, Any]:
    """
    Generate a complete survey response for a panelist using JSON objects directly.
    
    Designed for multithreading and Selenium automation with vignette-based tasks.
    This function rates entire vignettes (combinations of elements), not individual elements.
    Failed AI calls are parked and retried 3 times at lower concurrency before fallback.
    """
    # Initialize AI respondent
    ai_respondent = AIRespondent(openai_api_key=openai_api_key, model=model)
    
    # Build persona prompt
    persona_prompt = ai_respondent.build_persona_prompt(panelist_json, study_data)
    
    # Get panelist number to find tasks
    panelist_number = panelist_json.get('panelist_number')
    if panelist_number is None:
        raise ValueError("panelist_json must contain 'panelist_number'")
    
    # Get tasks for this panelist
    panelist_tasks = tasks_json.get(str(panelist_number), [])
    
    if not panelist_tasks:
        raise ValueError(f"No tasks found for panelist number {panelist_number}")

    pending: List[Dict[str, Any]] = [
        {"idx": idx, "task": task, "exclude_urls": set()}
        for idx, task in enumerate(panelist_tasks)
    ]
    results: Dict[int, Dict[str, Any]] = {}
    retry_passes, retry_workers, retry_backoff = _retry_settings(study_data if isinstance(study_data, dict) else {})

    completed, pending = _run_rating_pass(
        ai_respondent,
        pending,
        persona_prompt,
        study_data,
        panelist_number,
        max_vignette_workers,
    )
    results.update(completed)

    for pass_index in range(retry_passes):
        if not pending:
            break
        delay = retry_backoff[min(pass_index, len(retry_backoff) - 1)] if retry_backoff else 0.0
        if delay > 0:
            time.sleep(delay)
        workers = retry_workers[min(pass_index, len(retry_workers) - 1)] if retry_workers else 1
        logger.info(
            "Retrying %s parked vignette(s) for panelist %s (pass %s/%s, workers=%s)",
            len(pending),
            panelist_number,
            pass_index + 1,
            retry_passes,
            workers,
        )
        completed, pending = _run_rating_pass(
            ai_respondent,
            pending,
            persona_prompt,
            study_data,
            panelist_number,
            workers,
        )
        results.update(completed)

    for item in pending:
        fallback = ai_respondent._generate_fallback_vignette_rating(
            item["task"], persona_prompt, study_data
        )
        kind = item.get("last_kind") or "other"
        fallback["reasoning"] = (
            f"Fallback heuristic rating after {retry_passes} retries ({kind})"
        )
        results[item["idx"]] = _task_result_shell(
            panelist_number, item["idx"], item["task"], study_data, fallback
        )

    task_ratings = [results[i] for i in sorted(results.keys())]
    for tr in task_ratings:
        tr.pop("_original_index", None)
    
    # Extract classification answers in a clean format
    classification_answers = {}
    for q_id, answer_data in panelist_json.get('answers', {}).items():
        classification_answers[q_id] = {
            'question_id': q_id,
            'question_text': answer_data['question_text'],
            'answer': answer_data['answer'],
            'answer_index': answer_data['answer_index']
        }
    
    # Format for Selenium automation
    selenium_data = {
        'classification_answers': classification_answers,
        'task_ratings': [
            {
                'task_id': tr['task_id'],
                'task_index': tr['task_index'],
                'main_question': tr.get('main_question', ''),
                'vignette_content': tr.get('vignette_content', ''),
                'rating': tr['rating']
            }
            for tr in task_ratings
        ],
        'study_id': study_data.get('id'),
        'rating_scale': study_data.get('rating_scale', {'min_value': 1, 'max_value': 5})
    }
    
    return {
        'panelist_id': panelist_json.get('panelist_id'),
        'panelist_number': panelist_json.get('panelist_number'),
        'classification_answers': classification_answers,
        'task_ratings': task_ratings,
        'ready_for_selenium': selenium_data,
        'study_id': study_data.get('id'),
        'study_title': study_data.get('title'),
        'total_tasks': len(task_ratings),
        'generated_at': datetime.now(timezone.utc).isoformat()
    }


def process_panelist_response(
    panelist_json: Dict[str, Any],
    tasks_json: Dict[str, List[Dict[str, Any]]],
    study_data: Dict[str, Any],
    openai_api_key: Optional[str] = None,
    model: Optional[str] = None,
    max_vignette_workers: int = 10
) -> Dict[str, Any]:
    """
    Main function to process a single panelist and generate survey response.
    
    This is a clean, simple interface that takes a panelist and their tasks,
    processes them, and returns the complete response ready for Selenium automation.
    """
    return generate_panelist_response_from_json(
        panelist_json=panelist_json,
        tasks_json=tasks_json,
        study_data=study_data,
        openai_api_key=openai_api_key,
        model=model,
        max_vignette_workers=max_vignette_workers
    )
