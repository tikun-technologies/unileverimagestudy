"""
Verified analytics assistant orchestrator.

GPT-4o-mini only produces a tiny JSON query plan.
All numbers, rankings, and designs come from deterministic tools.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.cache import RedisCache
from app.core.config import settings
from app.core.redis import get_sync_redis
from app.models.user_model import User
from app.schemas.assistant_schema import (
    AssistantFollowUpContext,
    AssistantMetric,
    AssistantQueryPlan,
    AssistantQueryRequest,
    AssistantQueryResponse,
    AssistantToolName,
    CompareMode,
    RankDirection,
)
from app.services.analysis_filter import get_active_filter
from app.services.analysis_settings import get_study_analysis_settings
from app.services.assistant_agent import AgentUnavailable, run_agent_query
from app.services.assistant_message_service import (
    get_assistant_reply_for_parent,
    get_or_create_conversation,
    insert_assistant_message,
    insert_user_message,
    response_from_assistant_message,
)
from app.services.assistant_tools import (
    AssistantToolError,
    authorize_study_for_assistant,
    build_applied_context,
    execute_tool,
    extract_age_segment_from_text,
    extract_compare_pair,
    extract_gender_from_text,
    find_classification_question_by_text,
    infer_compare_mode,
    is_best_vs_worst_design_query,
    is_compare_intent,
    is_compare_top_designs_query,
    load_analysis_for_assistant,
    match_classification_options_in_text,
    resolve_age_segment_key,
    resolve_classification_question_from_options,
)

logger = logging.getLogger(__name__)

try:
    from openai import OpenAI  # type: ignore

    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


PLANNER_SYSTEM = """You convert study analytics questions into a tiny JSON query plan.
Return ONLY valid JSON with keys:
tool, metric, direction, limit, segment_section, segment_key, gender_key, age_key,
classification_question, classification_options, compare_mode, compare_left, compare_right,
must_include, clarification_prompt, clarification_options, unsupported_reason, confidence.

Allowed tools:
greeting, study_overview, classification_distribution, rank_elements, rank_designs,
compare, compare_segments, executive_summary, use_avoid_elements, response_time_summary, fatigue_summary,
explain_mindset, explain_design, list_saved_designs, clarify, unsupported.

Your job is INTENT understanding. Clients paraphrase freely — slang, typos, and
informal wording are normal. Map MEANING, never require exact keywords.

Rules:
- metric must be T, B, or R (default T).
- direction must be highest or lowest.
- A singular request ("best design", "best statement", "winning mix", "bestie design") has limit=1.
- A plural request without a number has limit=4.
- An explicit count must be respected: digits ("top 10", "top 2-3" → 3) and words
  ("top two", "best three designs", "five worst") → that count.
- must_include is an array of required design ingredients from the user question.
  Copy strings from the user question exactly; do not invent or shorten element names
  (keep "A6-silver-clinical", never reduce it to "silver").
  NEVER put ranking adjectives in must_include (highest-scoring, top-performing,
  best, worst, etc.). Those set direction/limit, not ingredients.
- Never invent numbers or element names.
- "What is the highest-scoring combination?" / "best mix" / "winning package"
  → tool=rank_designs, limit=1, direction=highest, must_include=[].
- Soft synonyms for ranking polarity: bestie/fave/favorite/winner ≈ best;
  loser/least/bottom ≈ worst. Soft synonyms for gender: men/guys/males ≈ Male;
  women/ladies/females ≈ Female.
- If the request is ambiguous between designs vs elements, or two valid intents, use
  tool=clarify with a short clarification_prompt and 2-5 clarification_options the user
  can tap (e.g. "Best 2 designs", "Top 2 elements", "Compare best vs worst").
- If outside verified study analytics/design, use tool=unsupported.
- confidence: 0.85-1.0 when intent is clear; 0.5-0.7 when guessed; below 0.45 only with clarify.

Tool mapping (paraphrases count as the same intent):
- rank_designs: designs / mixes / combinations / stacks / packages / creatives that win or lose.
  Examples → tool=rank_designs:
  - "top two designs" / "best 2 designs" / "show me the two best mixes" → limit=2, direction=highest
  - "worst three combinations" / "least performing designs" → limit=3, direction=lowest
  - "winning package" / "best overall design" / "what's the bestie design" → limit=1
  - "designs with A6-silver-clinical" → must_include=["A6-silver-clinical"]
  - "best design for people who selected Daily or almost daily"
    → classification_options=["Daily or almost daily"]
- rank_elements: statements / elements / concepts / claims / ingredients by themselves.
- explain_design: why a design wins / is better / beats the runner-up.
- compare: ANY side-by-side of two sides. Trigger words include compare, difference,
  differences between, contrast, versus, vs, against, compared to, how do X and Y differ,
  side by side — wording does not need to include the word "compare".
  - "Male vs Female", "men versus women", "difference between male and female"
    → compare_mode=segment, compare_left=Male, compare_right=Female
  - "difference between male best design and female best design"
    / "male bestie vs female bestie" / "how does male best differ from female best"
    → compare_mode=segment, left=Male, right=Female (best design per segment)
  - "18-24 vs 45-54" / "difference between 18-24 and 45-54" → compare_mode=segment
  - "Design #1 vs Design #2" / "first vs second design" → compare_mode=design
  - "compare top two designs" / "compare the best 2 designs" → compare_mode=design,
    compare_left="design 1", compare_right="design 2" (side-by-side of the top 2, NOT a list of 4)
  - "best vs worst design" / "difference between best and worst"
    → compare_mode=design, left=best, right=worst
  - two classification options → compare_mode=classification
- NEVER map a two-segment ask (Male vs Female, two ages) to Design #1 vs Design #2.
- Do NOT use rank_designs when the user wants a side-by-side / difference — use tool=compare.
- compare_segments: compare ALL segments in Gender/Age/Mindsets at once.
- executive_summary: key findings / stakeholder summary / “most important findings”.
- classification_distribution: how many answered/selected/chose a prelim option.
- use_avoid_elements: what to use or avoid.
- greeting: hi/hello/hey/thanks.
- clarify: when you truly cannot tell what they want.

Classification examples:
- "how many selected A few times per month" -> classification_options=["A few times per month"]
- "how many selected Daily or almost daily in male segment 22 years old"
  -> classification_options=["Daily or almost daily"], gender_key="Male", age_key="18-24"
- Options can repeat across questions; if ambiguous, set classification_options and leave
  classification_question null so the tool can clarify.
- gender_key is Male/Female; age_key is a study bucket (18-24, 25-34, ...).
"""


def _openai_client():
    if not OPENAI_AVAILABLE:
        return None
    api_key = settings.OPENAI_API_KEY or os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    return OpenAI(api_key=api_key)


_RATE_BUCKET: Dict[str, list] = {}

# Dedicated pool so the LLM planner (network I/O) overlaps the analysis build.
# Small and bounded — planner calls are short-lived and I/O-bound.
_PLANNER_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="assistant-planner")


def _rate_limit_ok_local(user_id: str) -> bool:
    now = time.time()
    window = 60.0
    limit = settings.ASSISTANT_RATE_LIMIT_PER_MINUTE
    bucket = _RATE_BUCKET.setdefault(user_id, [])
    _RATE_BUCKET[user_id] = [t for t in bucket if now - t < window]
    if len(_RATE_BUCKET[user_id]) >= limit:
        return False
    _RATE_BUCKET[user_id].append(now)
    return True


def _rate_limit_ok(user_id: str) -> bool:
    """
    Fixed-window rate limit shared across workers via Redis (atomic INCR).

    Falls back to the in-process limiter when Redis is unavailable so a single
    worker is still protected.
    """
    limit = settings.ASSISTANT_RATE_LIMIT_PER_MINUTE
    try:
        client = get_sync_redis()
        if client is not None:
            window_bucket = int(time.time() // 60)
            key = f"assistant:ratelimit:{user_id}:{window_bucket}"
            count = client.incr(key)
            if count == 1:
                # First hit in this window — expire slightly after the window.
                client.expire(key, 70)
            return int(count) <= limit
    except Exception as exc:
        logger.debug("Redis rate limit failed, using local limiter: %s", exc)
    return _rate_limit_ok_local(user_id)


def _cache_key(study_id: str, user_id: str, payload: Dict[str, Any]) -> str:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]
    return f"assistant:query:{study_id}:{user_id}:{digest}"


def _is_executive_summary_query(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "executive summary",
            "most important findings",
            "important findings",
            "key findings",
            "stakeholder summary",
            "summarize this study",
            "summary of findings",
        )
    )


def _is_cohort_rank_query(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:best|top|worst|leading)\s+(?:overall\s+)?(?:design|mix|combination)s?\b",
            text,
        )
        and any(
            token in text
            for token in (
                "selected",
                "chose",
                "chosen",
                "who selected",
                "who chose",
                "people who",
                "respondents who",
                "users who",
                "for those who",
                "for people who",
            )
        )
    )


def _apply_compare_plan(message: str, plan: AssistantQueryPlan, study_obj: Any) -> AssistantQueryPlan:
    if is_compare_top_designs_query(message):
        plan.compare_left = "design 1"
        plan.compare_right = "design 2"
        plan.compare_mode = CompareMode.design
        plan.tool = AssistantToolName.compare
        plan.limit = 2
        return plan
    if is_best_vs_worst_design_query(message):
        mode, left, right = infer_compare_mode(message, study_obj, plan)
        if mode == CompareMode.design and left == "best" and right == "worst":
            plan.compare_left = "best"
            plan.compare_right = "worst"
            plan.compare_mode = CompareMode.design
            plan.tool = AssistantToolName.compare
            return plan
    pair = extract_compare_pair(message)
    if not pair:
        # Still resolve mode from the full message when possible.
        mode, left, right = infer_compare_mode(message, study_obj, plan)
        if mode and left and right:
            plan.compare_left = str(left)[:120]
            plan.compare_right = str(right)[:120]
            plan.compare_mode = mode
            plan.tool = AssistantToolName.compare
        return plan
    left, right = pair
    plan.compare_left = left[:120]
    plan.compare_right = right[:120]
    mode, _, _ = infer_compare_mode(message, study_obj, plan)
    plan.compare_mode = mode
    plan.tool = AssistantToolName.compare
    return plan


def _deterministic_plan(message: str, request: AssistantQueryRequest) -> AssistantQueryPlan:
    text = message.casefold()
    follow = request.follow_up

    if re.fullmatch(
        r"\s*(?:hi|hello|hey|good\s+(?:morning|afternoon|evening)|thanks|thank\s+you)[!?.\s]*",
        text,
    ):
        return AssistantQueryPlan(tool=AssistantToolName.greeting, confidence=1.0)

    # Follow-up shortcuts
    if follow and any(token in text for token in ("opposite", "worst", "least", "now lowest", "now highest")):
        direction = RankDirection.lowest
        if follow.last_direction == RankDirection.lowest or "highest" in text or "best" in text:
            if "worst" in text or "least" in text or "lowest" in text:
                direction = RankDirection.lowest
            else:
                direction = RankDirection.highest
        if follow.last_direction == RankDirection.highest and ("opposite" in text or "worst" in text or "least" in text):
            direction = RankDirection.lowest
        if follow.last_direction == RankDirection.lowest and ("opposite" in text or "best" in text or "highest" in text):
            direction = RankDirection.highest
        tool = follow.last_tool or AssistantToolName.rank_elements
        return AssistantQueryPlan(
            tool=tool if tool not in {AssistantToolName.clarify, AssistantToolName.unsupported} else AssistantToolName.rank_elements,
            metric=follow.metric or request.metric or AssistantMetric.T,
            direction=direction,
            limit=follow.last_limit or 10,
            segment_section=follow.segment_section or request.segment_section,
            segment_key=follow.segment_key or request.segment_key,
            classification_question=follow.classification_question,
            confidence=0.85,
        )

    limit = 4
    explicit_limit = _extract_result_count(text)
    if explicit_limit:
        limit = explicit_limit

    direction = RankDirection.highest
    direction_text = text.replace("bottom up", "")
    if any(token in direction_text for token in ("worst", "least", "bottom", "lowest", "poor", "underperform")):
        direction = RankDirection.lowest

    metric = request.metric or AssistantMetric.T
    if "bottom up" in text or re.search(r"\b\(b\)\b", text) or re.search(r"\bmetric b\b", text):
        metric = AssistantMetric.B
    if "response time" in text or re.search(r"\b\(r\)\b", text):
        if "fatigue" not in text and "summary" not in text and "distribution" not in text:
            # keep T unless explicitly asking for R metric rankings
            if any(token in text for token in ("metric r", "using r", "response metric", "(r)")):
                metric = AssistantMetric.R

    segment_section = request.segment_section
    segment_key = request.segment_key
    gender_key = extract_gender_from_text(text)
    age_key = None
    if re.search(r"\d{2}-\d{2}|\b65\+|\bage\b|\baged\b|\byears?\s*old\b|\byo\b", text):
        age_key = extract_age_segment_from_text(text)

    if "mindset" in text:
        segment_section = "Mindsets"
        mk = re.search(r"mindset\s*([123])", text)
        if mk:
            n = mk.group(1)
            segment_key = f"Mindset_{n}_of_3" if "of 3" in text or "3 mindset" in text else f"Mindset_{n}_of_2"
    elif gender_key and not age_key:
        segment_section = segment_section or "Gender"
        segment_key = segment_key or gender_key
    elif age_key and not gender_key:
        segment_section = segment_section or "Age"
        segment_key = segment_key or age_key
    elif gender_key and age_key:
        # Dual demographic filter (common for classification). Keep a primary label too.
        segment_section = segment_section or "Gender"
        segment_key = segment_key or gender_key

    if _is_executive_summary_query(text):
        return AssistantQueryPlan(tool=AssistantToolName.executive_summary, confidence=0.95)

    if is_compare_intent(message):
        plan = AssistantQueryPlan(
            tool=AssistantToolName.compare,
            metric=metric,
            segment_section=segment_section,
            segment_key=segment_key,
            gender_key=gender_key,
            age_key=age_key,
            confidence=0.9,
        )
        pair = extract_compare_pair(message)
        if pair:
            plan.compare_left, plan.compare_right = pair[0][:120], pair[1][:120]
        # Narrow override: only explicit "top N designs" → Design #1 vs #2.
        if is_compare_top_designs_query(text):
            plan.compare_left = "design 1"
            plan.compare_right = "design 2"
            plan.compare_mode = CompareMode.design
            plan.limit = 2
            return plan
        # Force polarity only when this is truly best-vs-worst, not "best for A vs B".
        if is_best_vs_worst_design_query(text):
            left_side = plan.compare_left
            right_side = plan.compare_right
            segment_pair = bool(
                left_side
                and right_side
                and (
                    (extract_gender_from_text(left_side) and extract_gender_from_text(right_side))
                    or (
                        (extract_age_segment_from_text(left_side) or resolve_age_segment_key(left_side))
                        and (extract_age_segment_from_text(right_side) or resolve_age_segment_key(right_side))
                    )
                )
            )
            if not segment_pair:
                plan.compare_left = "best"
                plan.compare_right = "worst"
                plan.compare_mode = CompareMode.design
                return plan
        # Male best vs Female best (or two ages) → segment compare; never a single gender filter.
        left_g = extract_gender_from_text(plan.compare_left or "")
        right_g = extract_gender_from_text(plan.compare_right or "")
        left_a = extract_age_segment_from_text(plan.compare_left or "") or resolve_age_segment_key(
            plan.compare_left or ""
        )
        right_a = extract_age_segment_from_text(plan.compare_right or "") or resolve_age_segment_key(
            plan.compare_right or ""
        )
        if left_g and right_g and left_g != right_g:
            plan.compare_mode = CompareMode.segment
            plan.compare_left = left_g
            plan.compare_right = right_g
            plan.gender_key = None
            plan.segment_section = None
            plan.segment_key = None
        elif left_a and right_a and left_a != right_a:
            plan.compare_mode = CompareMode.segment
            plan.compare_left = left_a
            plan.compare_right = right_a
            plan.age_key = None
            plan.segment_section = None
            plan.segment_key = None
        return plan

    if _is_cohort_rank_query(text):
        return AssistantQueryPlan(
            tool=AssistantToolName.rank_designs,
            metric=metric,
            direction=direction,
            limit=explicit_limit or 1,
            segment_section=segment_section,
            segment_key=segment_key,
            gender_key=gender_key,
            age_key=age_key,
            confidence=0.9,
        )

    # Classification / prelim option counts.
    # Also continue a clarification turn when the previous tool was classification.
    classification_follow = bool(
        follow
        and follow.last_tool == AssistantToolName.classification_distribution
    )
    if classification_follow or any(
        token in text
        for token in (
            "how many answered",
            "how many selected",
            "how many chose",
            "how many picked",
            "how many users answered",
            "how many user answered",
            "classification",
            "prelim",
            "what are the other options",
            "answer options",
            "option counts",
            "selected this option",
            "answered this option",
            "classification option",
        )
    ):
        pending_options = list(follow.classification_options or []) if follow else []
        question_hint = follow.classification_question if follow else None
        # Clicking a clarification chip sends the question title as the message.
        if classification_follow and not any(
            token in text
            for token in (
                "how many",
                "classification",
                "prelim",
                "option",
                "selected",
                "answered",
                "chose",
            )
        ):
            question_hint = message.strip()
        follow_gender = follow.gender_key if follow else None
        follow_age = follow.age_key if follow else None
        return AssistantQueryPlan(
            tool=AssistantToolName.classification_distribution,
            metric=metric,
            segment_section=segment_section,
            segment_key=segment_key,
            gender_key=gender_key or follow_gender,
            age_key=age_key or follow_age,
            classification_question=question_hint,
            classification_options=pending_options,
            confidence=0.9,
        )
    if (
        any(token in text for token in ("why", "explain", "reason"))
        and any(token in text for token in ("design", "mix", "combination", "this one", "this"))
        and "mindset" not in text
    ):
        return AssistantQueryPlan(
            tool=AssistantToolName.explain_design,
            metric=metric,
            direction=direction,
            limit=2,
            segment_section=segment_section,
            segment_key=segment_key,
            confidence=0.9,
        )
    must_include = _extract_must_include(message)
    design_intent = any(
        token in text
        for token in (
            "best mix",
            "best design",
            "top design",
            "top designs",
            "best designs",
            "worst design",
            "worst designs",
            "least performing design",
            "least performing designs",
            "winning design",
            "winning mix",
            "winning package",
            "combinations",
            "configurator",
            "performing combination",
            "performing combinations",
            "performing design",
            "performing designs",
            "performing mix",
            "scoring combination",
            "scoring design",
            "scoring mix",
            "highest-scoring",
            "highest scoring",
            "lowest-scoring",
            "lowest scoring",
            "creative",
            "creatives",
            "package",
            "packages",
            "stack",
            "stacks",
        )
    ) or bool(
        re.search(
            r"\b(?:top|best|worst|lowest|highest)\s+(?:two|three|four|five|\d{1,2})\s+"
            r"(?:design|designs|mix|mixes|combination|combinations|package|packages)\b",
            text,
        )
    ) or bool(
        re.search(
            r"\b(?:highest|lowest|best|worst|top)[- ](?:scoring|performing)\s+"
            r"(?:design|mix|combination|package|stack|creative)s?\b",
            text,
        )
    )
    if design_intent or (
        must_include
        and any(
            token in text
            for token in (
                "with",
                "including",
                "using",
                "contain",
                "containing",
                "combination",
                "design",
                "mix",
                "performing",
            )
        )
    ):
        return AssistantQueryPlan(
            tool=AssistantToolName.rank_designs,
            metric=metric,
            direction=direction,
            limit=limit,
            segment_section=segment_section,
            segment_key=segment_key,
            must_include=must_include,
            confidence=0.92,
        )
    if any(token in text for token in ("use or avoid", "use/avoid", "should we use", "recommend elements")):
        return AssistantQueryPlan(
            tool=AssistantToolName.use_avoid_elements,
            metric=metric,
            segment_section=segment_section,
            segment_key=segment_key,
            confidence=0.88,
        )
    if "saved design" in text:
        return AssistantQueryPlan(tool=AssistantToolName.list_saved_designs, confidence=0.9)
    if "fatigue" in text:
        return AssistantQueryPlan(tool=AssistantToolName.fatigue_summary, confidence=0.9)
    if "response time" in text:
        return AssistantQueryPlan(tool=AssistantToolName.response_time_summary, confidence=0.9)
    if "explain mindset" in text or ("mindset" in text and "explain" in text):
        return AssistantQueryPlan(
            tool=AssistantToolName.explain_mindset,
            metric=metric,
            segment_section="Mindsets",
            segment_key=segment_key,
            confidence=0.9,
        )
    if any(token in text for token in ("compare all", "all segments", "every segment")) and any(
        token in text for token in ("gender", "age", "mindset", "segment")
    ):
        return AssistantQueryPlan(
            tool=AssistantToolName.compare_segments,
            metric=metric,
            segment_section=segment_section or ("Mindsets" if "mindset" in text else "Gender" if "gender" in text else "Age"),
            confidence=0.86,
        )
    if any(token in text for token in ("top element", "bottom element", "best element", "worst element", "top concept", "performers", "claims")):
        return AssistantQueryPlan(
            tool=AssistantToolName.rank_elements,
            metric=metric,
            direction=direction,
            limit=limit,
            segment_section=segment_section,
            segment_key=segment_key,
            confidence=0.9,
        )
    if any(token in text for token in ("overview", "how many respondents", "kpi", "dashboard")) or (
        "summary" in text and not _is_executive_summary_query(text)
    ):
        return AssistantQueryPlan(tool=AssistantToolName.study_overview, confidence=0.9)

    # Generic ranking language
    if any(token in text for token in ("top ", "best ", "worst ", "bottom ")):
        if "design" in text or "mix" in text or "combination" in text or must_include:
            tool = AssistantToolName.rank_designs
        else:
            tool = AssistantToolName.rank_elements
        return AssistantQueryPlan(
            tool=tool,
            metric=metric,
            direction=direction,
            limit=limit,
            segment_section=segment_section,
            segment_key=segment_key,
            must_include=must_include if tool == AssistantToolName.rank_designs else [],
            confidence=0.75,
        )

    return AssistantQueryPlan(
        tool=AssistantToolName.clarify,
        clarification_prompt="I’m not sure what you mean — what should I analyze?",
        clarification_options=[
            "Best design overall",
            "Top 2 designs",
            "Top 10 elements",
            "Compare best vs worst design",
            "Study overview",
            "Classification answer counts",
        ],
        confidence=0.35,
    )


def _parse_plan(raw: Dict[str, Any], request: AssistantQueryRequest) -> AssistantQueryPlan:
    tool_raw = str(raw.get("tool") or "clarify").strip()
    try:
        tool = AssistantToolName(tool_raw)
    except ValueError:
        tool = AssistantToolName.clarify

    metric_raw = str(raw.get("metric") or (request.metric.value if request.metric else "T")).upper()
    if metric_raw not in {"T", "B", "R"}:
        metric_raw = "T"

    direction_raw = str(raw.get("direction") or "highest").lower()
    if direction_raw not in {"highest", "lowest"}:
        direction_raw = "highest"

    try:
        limit = int(raw.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, settings.ASSISTANT_MAX_RESULT_LIMIT))

    options = raw.get("clarification_options") or []
    if not isinstance(options, list):
        options = []
    options = [str(o)[:120] for o in options[:8]]

    confidence_raw = raw.get("confidence")
    try:
        confidence = float(confidence_raw or 0.7)
    except (TypeError, ValueError):
        confidence = {
            "high": 0.9,
            "medium": 0.7,
            "low": 0.4,
        }.get(str(confidence_raw or "").casefold(), 0.7)

    must_include_raw = raw.get("must_include") or []
    if isinstance(must_include_raw, str):
        must_include_raw = [must_include_raw]
    if not isinstance(must_include_raw, list):
        must_include_raw = []
    must_include = [str(item)[:120] for item in must_include_raw if str(item or "").strip()][:8]

    classification_options_raw = raw.get("classification_options") or []
    if isinstance(classification_options_raw, str):
        classification_options_raw = [classification_options_raw]
    if not isinstance(classification_options_raw, list):
        classification_options_raw = []
    classification_options = [
        str(item)[:120] for item in classification_options_raw if str(item or "").strip()
    ][:8]
    if not classification_options and request.follow_up:
        classification_options = list(request.follow_up.classification_options or [])[:8]

    gender_key = None
    if raw.get("gender_key"):
        gender_key = extract_gender_from_text(str(raw.get("gender_key"))) or str(raw.get("gender_key"))[:40]
    elif request.follow_up and request.follow_up.gender_key:
        gender_key = request.follow_up.gender_key

    age_key = None
    if raw.get("age_key"):
        age_key = resolve_age_segment_key(str(raw.get("age_key"))) or str(raw.get("age_key"))[:40]
    elif request.follow_up and request.follow_up.age_key:
        age_key = request.follow_up.age_key

    return AssistantQueryPlan(
        tool=tool,
        metric=AssistantMetric(metric_raw),
        direction=RankDirection(direction_raw),
        limit=limit,
        segment_section=(str(raw["segment_section"])[:80] if raw.get("segment_section") else request.segment_section),
        segment_key=(str(raw["segment_key"])[:80] if raw.get("segment_key") else request.segment_key),
        gender_key=gender_key,
        age_key=age_key,
        classification_question=(
            str(raw["classification_question"])[:200]
            if raw.get("classification_question")
            else (request.follow_up.classification_question if request.follow_up else None)
        ),
        classification_options=classification_options,
        compare_mode=(
            CompareMode(str(raw["compare_mode"]))
            if raw.get("compare_mode") in {m.value for m in CompareMode}
            else None
        ),
        compare_left=(str(raw["compare_left"])[:120] if raw.get("compare_left") else None),
        compare_right=(str(raw["compare_right"])[:120] if raw.get("compare_right") else None),
        must_include=must_include,
        clarification_prompt=(str(raw.get("clarification_prompt") or "")[:400] or None),
        clarification_options=options,
        unsupported_reason=(str(raw.get("unsupported_reason") or "")[:400] or None),
        confidence=max(0.0, min(1.0, confidence)),
    )


_WORD_COUNTS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


def _extract_result_count(text: str) -> Optional[int]:
    """Parse explicit result counts from natural language ranking requests."""
    if not text:
        return None
    normalized = text.casefold()

    range_match = re.search(
        r"\b(?:top|best|worst|bottom|lowest|highest|least|show|give(?:\s+me)?)\s+"
        r"(\d{1,2})\s*(?:[-–]|to)\s*(\d{1,2})\b",
        normalized,
    )
    if range_match:
        upper = max(int(range_match.group(1)), int(range_match.group(2)))
        return max(1, min(upper, settings.ASSISTANT_MAX_RESULT_LIMIT))

    word_alt = "|".join(_WORD_COUNTS.keys())
    word_patterns = (
        rf"\b(?:top|best|worst|bottom|lowest|highest|least)\s+(?:performing\s+)?({word_alt})\b",
        rf"\b({word_alt})\s+(?:top|best|worst|bottom|lowest|highest|least)\s+"
        rf"(?:performing\s+)?(?:design|designs|mix|mixes|combination|combinations|"
        rf"element|elements|statement|statements)\b",
        rf"\b(?:give(?:\s+me)?|show(?:\s+me)?|get)\s+(?:the\s+)?({word_alt})\s+"
        rf"(?:top|best|worst|bottom|lowest|highest|least)\b",
    )
    for pattern in word_patterns:
        match = re.search(pattern, normalized)
        if match:
            value = _WORD_COUNTS.get(match.group(1))
            if value:
                return max(1, min(value, settings.ASSISTANT_MAX_RESULT_LIMIT))

    patterns = (
        # "worst performing 5", "best performing 10"
        r"\b(?:top|best|worst|bottom|lowest|highest|least)\s+performing\s+(\d{1,2})\b",
        # "top 5", "worst 5", "best 10"
        r"\b(?:top|best|worst|bottom|lowest|highest|show|give(?:\s+me)?)\s+(\d{1,2})\b",
        # "least performing 5", "least-performing 5"
        r"\bleast(?:-|\s)?performing\s+(\d{1,2})\b",
        # "5 worst designs", "5 best performing combinations"
        r"\b(\d{1,2})\s+(?:top|best|worst|bottom|lowest|highest|least)\s+(?:performing\s+)?"
        r"(?:design|designs|mix|mixes|combination|combinations|element|elements|statement|statements)?",
        # "give me the 5 best overall"
        r"\b(?:give(?:\s+me)?|show(?:\s+me)?|get)\s+(?:the\s+)?(\d{1,2})\s+"
        r"(?:top|best|worst|bottom|lowest|highest|least)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            return max(1, min(int(match.group(1)), settings.ASSISTANT_MAX_RESULT_LIMIT))
    return None


def _explicit_result_count(text: str) -> Optional[int]:
    return _extract_result_count(text)


_STOP_INCLUDE_WORDS = {
    "a",
    "an",
    "the",
    "with",
    "using",
    "including",
    "include",
    "that",
    "includes",
    "containing",
    "contains",
    "having",
    "have",
    "colour",
    "color",
    "colored",
    "coloured",
    "design",
    "designs",
    "mix",
    "mixes",
    "combination",
    "combinations",
    "package",
    "packages",
    "stack",
    "stacks",
    "creative",
    "creatives",
    "element",
    "elements",
    "layer",
    "and",
    "or",
    "for",
    "overall",
    "best",
    "bestie",
    "top",
    "worst",
    "lowest",
    "highest",
    "leading",
    "winning",
    "performing",
    "scoring",
    "score",
    "scored",
    "strongest",
    "weakest",
    "favorite",
    "favourite",
    "fave",
    "what",
    "which",
    "is",
    "are",
    "show",
    "give",
    "me",
}

# Hyphenated ranking adjectives that look like element codes but aren't
# (e.g. "highest-scoring", "top-performing").
_RANKING_ADJECTIVE_RE = re.compile(
    r"^(?:"
    r"(?:highest|lowest|best|worst|top|bottom|least|most|leading|winning)"
    r"[-_]?"
    r"(?:scoring|performing|rated|ranked|valued)?"
    r"|"
    r"(?:high|low)[-_]?scor(?:e|ing|ed)"
    r")$",
    flags=re.IGNORECASE,
)


def _is_ranking_phrase_token(value: str) -> bool:
    """True for polarity/ranking words that must never become must_include."""
    text = str(value or "").strip().casefold().replace("_", "-")
    if not text:
        return False
    if text in _STOP_INCLUDE_WORDS:
        return True
    if _RANKING_ADJECTIVE_RE.fullmatch(text):
        return True
    # "highest scoring" / "top performing" as multi-word leftovers
    if re.fullmatch(
        r"(?:highest|lowest|best|worst|top|bottom|least|most)\s+"
        r"(?:scoring|performing|rated|ranked)",
        text,
    ):
        return True
    return False


def _looks_like_element_code(value: str) -> bool:
    """True for codes like A6-silver-clinical / silver-clinical (not age ranges)."""
    text = str(value or "").strip()
    if not text or " " in text:
        return False
    if _is_ranking_phrase_token(text):
        return False
    if re.fullmatch(r"\d{1,3}\s*[-–]\s*\d{1,3}", text):
        return False
    if re.fullmatch(r"\d{1,3}\+?", text):
        return False
    # Letter/digit token with at least one hyphen, or code+digits like A6silver…
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+){1,6}", text):
        return True
    if re.fullmatch(r"[A-Za-z]{1,4}\d+[A-Za-z0-9_-]*", text):
        return True
    return False


def _extract_must_include(message: str) -> List[str]:
    """Pull required element/color ingredients from natural-language questions."""
    text = " ".join((message or "").strip().split())
    if not text:
        return []
    found: List[str] = []

    def add(raw: str) -> None:
        cleaned = " ".join(str(raw or "").strip(" .,!?;:\"'()[]").split())
        if not cleaned:
            return
        # Drop leading filler words but keep meaningful tokens like "white".
        parts = [
            p
            for p in re.split(r"\s+", cleaned)
            if p.casefold() not in _STOP_INCLUDE_WORDS and not _is_ranking_phrase_token(p)
        ]
        if not parts and cleaned.casefold() not in _STOP_INCLUDE_WORDS:
            parts = [cleaned]
        value = " ".join(parts).strip(" -_")
        if len(value) < 2:
            return
        # Never treat ranking adjectives as required ingredients.
        if _is_ranking_phrase_token(value):
            return
        # Avoid capturing whole question tails.
        if len(value) > 80:
            return
        if value.casefold() in {item.casefold() for item in found}:
            return
        found.append(value)

    for quoted in re.findall(r"[\"']([^\"']{2,80})[\"']", text):
        add(quoted)

    # Specific element codes mentioned anywhere (A6-silver-clinical, silver-clinical).
    for code in re.findall(
        r"\b[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+){1,6}\b|\b[A-Za-z]{1,4}\d+[A-Za-z0-9_-]*\b",
        text,
    ):
        if _looks_like_element_code(code):
            add(code)

    patterns = [
        r"\bwith\s+(?:a\s+|an\s+|the\s+)?(.+?)(?:\s+(?:for|among|using|in|on|under)\b|[?.!]|$)",
        r"\bincluding\s+(?:a\s+|an\s+|the\s+)?(.+?)(?:\s+(?:for|among|using|in|on|under)\b|[?.!]|$)",
        r"\bthat\s+includes?\s+(?:a\s+|an\s+|the\s+)?(.+?)(?:\s+(?:for|among|using|in|on|under)\b|[?.!]|$)",
        r"\b(?:which\s+)?(?:has|have)\s+(?:a\s+|an\s+|the\s+)?(.+?)(?:\s+(?:for|among|using|in|on|under)\b|[?.!]|$)",
        r"\bthat\s+has\s+(?:a\s+|an\s+|the\s+)?(.+?)(?:\s+(?:for|among|using|in|on|under)\b|[?.!]|$)",
        r"\busing\s+(?:the\s+)?(?:element\s+)?(.+?)(?:\s+(?:for|among|in|on|under)\b|[?.!]|$)",
        r"\bcontain(?:s|ing)?\s+(?:a\s+|an\s+|the\s+)?(.+?)(?:\s+(?:for|among|using|in|on|under)\b|[?.!]|$)",
        r"\bwhich\s+contain(?:s|ing)?\s+(?:a\s+|an\s+|the\s+)?(.+?)(?:\s+(?:for|among|using|in|on|under)\b|[?.!]|$)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            chunk = match.group(1)
            # Support "A and B" / "A, B".
            for piece in re.split(r"\s*(?:,|/|\band\b|\bplus\b)\s*", chunk, flags=re.IGNORECASE):
                add(piece)

    # Color shorthand: "white colour/color" or color used as an ingredient filter.
    # Skip when a specific hyphenated element code was already captured (avoid
    # collapsing "A6-silver-clinical" down to a loose "silver" match).
    has_specific_code = any(_looks_like_element_code(item) for item in found)
    color_match = re.search(
        r"\b(white|black|red|blue|green|yellow|orange|pink|purple|grey|gray|transparent|transp|opaque|silver|gold)\b",
        text,
        flags=re.IGNORECASE,
    )
    if (
        color_match
        and not has_specific_code
        and re.search(
            r"\b(?:colour|color|coloured|colored|with|including|contain(?:s|ing)?|has|have)\b",
            text,
            flags=re.IGNORECASE,
        )
    ):
        add(color_match.group(1))

    # Prefer longer/more-specific hints when one contains another
    # (e.g. keep A6-silver-clinical, drop silver).
    pruned: List[str] = []
    for item in sorted(found, key=lambda value: (-len(value), value.casefold())):
        item_norm = re.sub(r"[^a-z0-9]+", "", item.casefold())
        if any(item_norm and item_norm != re.sub(r"[^a-z0-9]+", "", other.casefold()) and item_norm in re.sub(r"[^a-z0-9]+", "", other.casefold()) for other in pruned):
            continue
        pruned.append(item)
    return pruned[:8]


def _normalize_plan_for_question(
    plan: AssistantQueryPlan,
    message: str,
    study_type: str,
    request: AssistantQueryRequest,
) -> AssistantQueryPlan:
    """Enforce product semantics after either OpenAI or fallback planning."""
    text = message.casefold()
    follow = request.follow_up

    if re.fullmatch(
        r"\s*(?:hi|hello|hey|good\s+(?:morning|afternoon|evening)|thanks|thank\s+you)[!?.\s]*",
        text,
    ):
        plan.tool = AssistantToolName.greeting
        plan.limit = 1
        return plan

    # Keep AI/deterministic clarification intact — do not remap to a guessed tool.
    if plan.tool == AssistantToolName.clarify:
        if not plan.clarification_prompt:
            plan.clarification_prompt = "I’m not sure what you mean — what should I analyze?"
        if not plan.clarification_options:
            plan.clarification_options = [
                "Best design overall",
                "Top 2 designs",
                "Top 10 elements",
                "Compare best vs worst design",
                "Study overview",
            ]
        return plan

    # No metric in the user's words always means T. UI tab state must not
    # silently change the meaning of an otherwise "overall" question.
    if "bottom up" in text or re.search(r"\bmetric\s*b\b|\busing\s+b\b|\(b\)", text):
        plan.metric = AssistantMetric.B
    elif re.search(r"\bmetric\s*r\b|\busing\s+r\b|\(r\)", text):
        plan.metric = AssistantMetric.R
    elif follow and any(token in text for token in ("same", "opposite", "why", "this", "now")):
        plan.metric = follow.metric or AssistantMetric.T
    else:
        plan.metric = AssistantMetric.T

    if re.search(r"\boverall\b", text):
        plan.segment_section = None
        plan.segment_key = None

    # Demographic filters: Gender and Age can both be present (classification).
    # Dual-gender / dual-age compares must NOT collapse to a single filter.
    dual_gender_compare = bool(
        re.search(r"\b(?:male|males|men|man)\b", text)
        and re.search(r"\b(?:female|females|women|woman)\b", text)
    )
    gender_from_text = None if dual_gender_compare else extract_gender_from_text(text)
    age_from_text = extract_age_segment_from_text(text)
    if gender_from_text:
        plan.gender_key = gender_from_text
    elif plan.gender_key and not dual_gender_compare:
        plan.gender_key = extract_gender_from_text(str(plan.gender_key)) or plan.gender_key
    if dual_gender_compare:
        plan.gender_key = None
        if str(plan.segment_section or "").casefold() == "gender":
            plan.segment_section = None
            plan.segment_key = None
    if age_from_text:
        plan.age_key = age_from_text
    elif plan.age_key:
        plan.age_key = resolve_age_segment_key(plan.age_key) or plan.age_key

    # Models often return colloquial labels ("men", "female") while analysis
    # sections use canonical keys. Normalize before deterministic lookup.
    requested_segment = str(plan.segment_key or "").strip()
    requested_lower = requested_segment.casefold()
    if dual_gender_compare:
        pass
    elif re.search(r"\b(?:men|man|male)\b", text) or requested_lower in {"men", "man", "males", "male"}:
        plan.gender_key = plan.gender_key or "Male"
        if not plan.age_key:
            plan.segment_section = "Gender"
            plan.segment_key = "Male"
    elif re.search(r"\b(?:women|woman|female)\b", text) or requested_lower in {"women", "woman", "females", "female"}:
        plan.gender_key = plan.gender_key or "Female"
        if not plan.age_key:
            plan.segment_section = "Gender"
            plan.segment_key = "Female"
    elif str(plan.segment_section or "").casefold() == "gender":
        plan.segment_section = "Gender"

    # Age: map "45" / "47" / "22 years old" onto analysis buckets.
    age_from_key = resolve_age_segment_key(plan.segment_key) if str(plan.segment_section or "").casefold() == "age" else None
    snapped_age = plan.age_key or age_from_text or age_from_key
    if snapped_age:
        plan.age_key = snapped_age
        if not plan.gender_key:
            plan.segment_section = "Age"
            plan.segment_key = snapped_age
        else:
            # Dual filter: keep Gender as primary segment label for chips.
            plan.segment_section = plan.segment_section or "Gender"
            plan.segment_key = plan.gender_key

    direction_text = text.replace("bottom up", "")
    if any(token in direction_text for token in ("worst", "least-performing", "least performing", "lowest", "underperform")):
        plan.direction = RankDirection.lowest
    elif any(token in direction_text for token in ("best", "top", "highest")):
        plan.direction = RankDirection.highest

    extracted_includes = _extract_must_include(message)
    if extracted_includes:
        # Prefer question text over model output for ingredient requirements.
        merged = list(extracted_includes)
        for item in plan.must_include or []:
            if item.casefold() not in {value.casefold() for value in merged}:
                merged.append(item)
        plan.must_include = merged[:8]
    elif plan.must_include:
        # Keep model must_include only when they look like real ingredients —
        # drop ranking adjectives the LLM sometimes copies from the question.
        plan.must_include = [
            item for item in plan.must_include if not _is_ranking_phrase_token(item)
        ][:8]
    else:
        plan.must_include = []
    # Final safety: strip ranking tokens even if both sources contributed them.
    plan.must_include = [
        item for item in (plan.must_include or []) if not _is_ranking_phrase_token(item)
    ][:8]
    if plan.must_include and plan.tool in {
        AssistantToolName.clarify,
        AssistantToolName.rank_elements,
        AssistantToolName.study_overview,
    }:
        # "top performing with <element>" is a design request.
        if any(
            token in text
            for token in (
                "with",
                "including",
                "using",
                "contain",
                "containing",
                "combination",
                "design",
                "mix",
                "performing",
            )
        ):
            plan.tool = AssistantToolName.rank_designs

    explicit_count = _explicit_result_count(text)
    # Singular best/worst design asks — covers "highest-scoring combination",
    # "what is the best mix", "winning package", etc.
    _design_noun = r"(?:design|mix|combination|package|stack|creative)"
    singular_design = bool(
        re.search(
            rf"\b(best|worst|top|lowest|highest)\s+(?:overall\s+)?{_design_noun}\b",
            text,
        )
        or re.search(
            rf"\b(?:best|worst|top|lowest|highest)[- ](?:scoring|performing|rated|ranked)\s+"
            rf"(?:overall\s+)?{_design_noun}\b",
            text,
        )
        or re.search(
            rf"\b(?:winning|leading|strongest|weakest)\s+{_design_noun}\b",
            text,
        )
        or re.search(
            rf"\b(?:which|what)\s+(?:is\s+)?(?:the\s+)?(?:best|worst|top|highest[- ]scoring|"
            rf"highest[- ]performing|lowest[- ]scoring)\s+{_design_noun}\b",
            text,
        )
        or re.search(
            rf"\b(?:which|what)\s+(?:{_design_noun})\s+is\s+(?:the\s+)?"
            rf"(?:best|worst|highest[- ]scoring|top[- ]performing)\b",
            text,
        )
        or (
            plan.must_include
            and re.search(r"\b(?:which|what)\s+is\s+(?:the\s+)?(?:top|best)\b", text)
            and not re.search(r"\b(?:statement|element|concept|claim)s?\b", text)
        )
    )
    singular_element = bool(
        re.search(r"\b(best|worst|top|lowest|highest)\s+(?:overall\s+)?(?:statement|element|concept|claim)\b", text)
        or re.search(
            r"\b(?:best|worst|top|lowest|highest)[- ](?:scoring|performing)\s+"
            r"(?:overall\s+)?(?:statement|element|concept|claim)\b",
            text,
        )
        or re.search(r"\bwhich\s+(?:statement|element|concept|claim)\s+is\s+(?:the\s+)?best\b", text)
    )
    plural_element = bool(re.search(r"\b(statements|elements|concepts|claims)\b", text))
    plural_design = bool(
        re.search(r"\b(designs|mixes|combinations|packages|stacks|creatives)\b", text)
    )

    if plan.tool == AssistantToolName.explain_design:
        plan.limit = 2
        return plan

    if _is_executive_summary_query(text):
        plan.tool = AssistantToolName.executive_summary
        return plan

    # Compare / difference / contrast — trust free-text intent. Static rules only
    # correct clear special cases (top N designs, best vs worst).
    if plan.tool == AssistantToolName.compare or is_compare_intent(message):
        plan.tool = AssistantToolName.compare
        # Prefer extracted sides from the question; keep model sides if extract finds none.
        pair = extract_compare_pair(message)
        if pair:
            plan.compare_left, plan.compare_right = pair[0][:120], pair[1][:120]
        if is_compare_top_designs_query(text):
            plan.compare_left = "design 1"
            plan.compare_right = "design 2"
            plan.compare_mode = CompareMode.design
            plan.limit = 2
            return plan
        if is_best_vs_worst_design_query(text):
            left_side = plan.compare_left
            right_side = plan.compare_right
            segment_pair = bool(
                left_side
                and right_side
                and (
                    (extract_gender_from_text(left_side) and extract_gender_from_text(right_side))
                    or (
                        (extract_age_segment_from_text(left_side) or resolve_age_segment_key(left_side))
                        and (extract_age_segment_from_text(right_side) or resolve_age_segment_key(right_side))
                    )
                )
            )
            if not segment_pair:
                plan.compare_left = "best"
                plan.compare_right = "worst"
                plan.compare_mode = CompareMode.design
                return plan
        left_g = extract_gender_from_text(plan.compare_left or "")
        right_g = extract_gender_from_text(plan.compare_right or "")
        left_a = extract_age_segment_from_text(plan.compare_left or "") or resolve_age_segment_key(
            plan.compare_left or ""
        )
        right_a = extract_age_segment_from_text(plan.compare_right or "") or resolve_age_segment_key(
            plan.compare_right or ""
        )
        if left_g and right_g and left_g != right_g:
            plan.compare_mode = CompareMode.segment
            plan.compare_left = left_g
            plan.compare_right = right_g
            plan.gender_key = None
            plan.segment_section = None
            plan.segment_key = None
        elif left_a and right_a and left_a != right_a:
            plan.compare_mode = CompareMode.segment
            plan.compare_left = left_a
            plan.compare_right = right_a
            plan.age_key = None
            plan.segment_section = None
            plan.segment_key = None
        return plan

    if _is_cohort_rank_query(text):
        plan.tool = AssistantToolName.rank_designs
        plan.limit = explicit_count or 1
        return plan

    if singular_design:
        plan.tool = AssistantToolName.rank_designs
        plan.limit = explicit_count or 1
    elif singular_element:
        plan.tool = AssistantToolName.rank_elements
        plan.limit = explicit_count or 1
    elif explicit_count and plan.tool in {AssistantToolName.rank_elements, AssistantToolName.rank_designs}:
        plan.limit = explicit_count
    elif plural_element and plan.tool == AssistantToolName.rank_elements and not explicit_count:
        plan.limit = 4
    elif plural_design and plan.tool == AssistantToolName.rank_designs and not explicit_count:
        plan.limit = 4

    # "Which is the best overall?" is naturally study-type dependent.
    # A layer study means the composite; a text study means one statement.
    vague_best_overall = bool(
        re.search(r"\b(?:which|what)\s+is\s+(?:the\s+)?best(?:\s+overall)?\b", text)
        or re.fullmatch(r"(?:show\s+me\s+)?(?:the\s+)?best\s+overall[?.!]?", text.strip())
    )
    if vague_best_overall and not singular_design and not singular_element:
        if study_type == "layer":
            plan.tool = AssistantToolName.rank_designs
        elif study_type == "text":
            plan.tool = AssistantToolName.rank_elements
        plan.limit = 1

    return plan


def plan_query(message: str, request: AssistantQueryRequest) -> Tuple[AssistantQueryPlan, Dict[str, Any]]:
    usage: Dict[str, Any] = {"planner": "deterministic", "prompt_tokens": 0, "completion_tokens": 0}
    client = _openai_client()
    if client is None:
        return _deterministic_plan(message, request), usage

    compact_context = {
        "metric_default": (request.metric.value if request.metric else "T"),
        "segment_section": request.segment_section,
        "segment_key": request.segment_key,
        "follow_up": request.follow_up.model_dump() if request.follow_up else None,
    }
    user_payload = {
        "question": message[:500],
        "context": compact_context,
    }
    try:
        response = client.chat.completions.create(
            model=settings.ASSISTANT_MODEL or settings.OPENAI_MODEL or "gpt-4o-mini",
            temperature=0,
            max_tokens=settings.ASSISTANT_MAX_OUTPUT_TOKENS,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": PLANNER_SYSTEM},
                {"role": "user", "content": json.dumps(user_payload, separators=(",", ":"))},
            ],
            timeout=settings.ASSISTANT_TIMEOUT_SECONDS,
        )
        content = response.choices[0].message.content or "{}"
        raw = json.loads(content)
        plan = _parse_plan(raw, request)
        usage = {
            "planner": "openai",
            "model": settings.ASSISTANT_MODEL or settings.OPENAI_MODEL or "gpt-4o-mini",
            "prompt_tokens": getattr(getattr(response, "usage", None), "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(getattr(response, "usage", None), "completion_tokens", 0) or 0,
        }
        # Prefer the model's clarification when it is unsure — that is the product
        # contract ("ask what you mean") instead of guessing wrongly.
        if plan.tool == AssistantToolName.clarify:
            if not plan.clarification_prompt:
                plan.clarification_prompt = "I’m not sure what you mean — what should I analyze?"
            if not plan.clarification_options:
                plan.clarification_options = [
                    "Best design overall",
                    "Top 2 designs",
                    "Top 10 elements",
                    "Compare best vs worst design",
                    "Study overview",
                ]
            return plan, usage
        # Low-confidence non-clarify guesses: try deterministic; if that is also
        # unsure, ask the user instead of executing a weak guess.
        if plan.confidence < 0.45:
            fallback = _deterministic_plan(message, request)
            if fallback.tool != AssistantToolName.clarify:
                plan = fallback
                usage["planner"] = "openai+deterministic_fallback"
            else:
                plan = AssistantQueryPlan(
                    tool=AssistantToolName.clarify,
                    clarification_prompt=(
                        plan.clarification_prompt
                        or fallback.clarification_prompt
                        or "I’m not sure what you mean — what should I analyze?"
                    ),
                    clarification_options=(
                        plan.clarification_options
                        or fallback.clarification_options
                        or [
                            "Best design overall",
                            "Top 2 designs",
                            "Top 10 elements",
                            "Compare best vs worst design",
                        ]
                    ),
                    confidence=0.35,
                )
                usage["planner"] = "openai+clarify"
        return plan, usage
    except Exception as exc:
        logger.warning("Assistant planner failed, using deterministic fallback: %s", exc)
        usage["planner_error"] = str(exc)[:200]
        return _deterministic_plan(message, request), usage


def _enrich_classification_plan_with_study(
    plan: AssistantQueryPlan,
    message: str,
    study_obj: Any,
    request: AssistantQueryRequest,
) -> AssistantQueryPlan:
    """Resolve classification question/options using the study's configured catalog."""
    follow = request.follow_up
    text = message.casefold()
    matched_question = find_classification_question_by_text(study_obj, message)
    matched_options = match_classification_options_in_text(study_obj, message)

    if plan.tool in {
        AssistantToolName.executive_summary,
        AssistantToolName.compare_segments,
    }:
        return plan

    if plan.tool == AssistantToolName.compare:
        return _apply_compare_plan(message, plan, study_obj)

    pending: List[str] = []
    for opt in list(plan.classification_options or []) + list(
        (follow.classification_options if follow else None) or []
    ) + matched_options:
        cleaned = " ".join(str(opt or "").strip().split())
        if cleaned and cleaned not in pending:
            pending.append(cleaned)
    plan.classification_options = pending[:8]

    # Preserve demographic filters across clarification turns.
    if follow:
        plan.gender_key = plan.gender_key or follow.gender_key
        plan.age_key = plan.age_key or follow.age_key
    if not plan.gender_key:
        plan.gender_key = extract_gender_from_text(message)
    if not plan.age_key:
        plan.age_key = extract_age_segment_from_text(message)

    if _is_cohort_rank_query(text) or (
        plan.tool == AssistantToolName.rank_designs and plan.classification_options
    ):
        plan.tool = AssistantToolName.rank_designs
        if not plan.classification_question and plan.classification_options:
            resolved, _ = resolve_classification_question_from_options(
                study_obj, plan.classification_options
            )
            if resolved:
                plan.classification_question = resolved.question_text
        return plan

    if matched_question and plan.tool not in {
        AssistantToolName.rank_designs,
        AssistantToolName.compare,
    }:
        plan.tool = AssistantToolName.classification_distribution
        plan.classification_question = matched_question.question_text
        return plan

    if follow and follow.last_tool == AssistantToolName.classification_distribution:
        plan.tool = AssistantToolName.classification_distribution
        # Clarification chip sends the full question title as the next message.
        if not plan.classification_question and message.strip():
            plan.classification_question = message.strip()[:200]
        return plan

    if matched_options:
        if _is_cohort_rank_query(text):
            plan.tool = AssistantToolName.rank_designs
            plan.limit = plan.limit or 1
            if not plan.classification_question:
                resolved, _ = resolve_classification_question_from_options(
                    study_obj, plan.classification_options or matched_options
                )
                if resolved:
                    plan.classification_question = resolved.question_text
            return plan
        plan.tool = AssistantToolName.classification_distribution
        # Options can repeat across questions — only auto-bind when unique.
        if not plan.classification_question:
            resolved, candidates = resolve_classification_question_from_options(
                study_obj, matched_options
            )
            if resolved:
                plan.classification_question = resolved.question_text
            elif candidates:
                # Leave question unset so the tool asks which question.
                plan.classification_question = None
        return plan

    if plan.tool == AssistantToolName.classification_distribution and not plan.classification_question:
        # Keep tool; tool layer will clarify which question when needed.
        return plan

    return plan


_GREETING_RE = re.compile(
    r"\s*(?:hi|hello|hey|good\s+(?:morning|afternoon|evening)|thanks|thank\s+you)[!?.\s]*",
)


def _is_simple_greeting(message: str) -> bool:
    """Bare pleasantries need no data lookup — answer them without the agent."""
    return bool(_GREETING_RE.fullmatch((message or "").casefold()))


def _has_responses(analysis: Optional[Dict[str, Any]]) -> bool:
    summary = (analysis or {}).get("dashboard_summary") or {}
    return int(summary.get("totalResponses") or 0) > 0


def _finalize_agent_response(
    db: Session,
    *,
    request_id: str,
    study_obj: Any,
    analysis: Dict[str, Any],
    filters: Optional[Dict[str, Any]],
    agent_result: Dict[str, Any],
    conversation: Any,
    user_message: Any,
    cache_key: str,
) -> AssistantQueryResponse:
    """Turn an agent result into the same response shape the planner produces."""
    primary_tool = agent_result.get("agent_primary_tool") or AssistantToolName.study_overview
    plan = agent_result.get("agent_plan") or AssistantQueryPlan(tool=primary_tool)
    plan.tool = primary_tool

    context = build_applied_context(study_obj, analysis, plan, filters)
    # An answer with no tool behind it (off-topic, or a question about the tool
    # itself) must not wear the verified badge.
    context.verified = bool(agent_result.get("agent_data_backed"))

    follow_up_ctx = agent_result.get("follow_up_context")
    follow_up_model = None
    if isinstance(follow_up_ctx, dict):
        try:
            follow_up_model = AssistantFollowUpContext(**follow_up_ctx)
        except Exception:
            follow_up_model = None

    response = AssistantQueryResponse(
        request_id=request_id,
        status="answered",
        answer_text=agent_result.get("answer_text") or "",
        tool=primary_tool,
        applied_context=context,
        blocks=agent_result.get("blocks") or [],
        evidence=agent_result.get("evidence") or [],
        follow_ups=agent_result.get("follow_ups") or [],
        actions=agent_result.get("actions") or [],
        clarification_options=[],
        follow_up_context=follow_up_model,
        usage=agent_result.get("usage") or {},
        conversation_id=conversation.id,
        user_message_id=user_message.id,
    )

    assistant_row = insert_assistant_message(
        db,
        conversation=conversation,
        parent_message=user_message,
        content=response.answer_text,
        response=response,
        status="complete",
    )
    response.assistant_message_id = assistant_row.id

    RedisCache.set(
        cache_key,
        json.loads(response.model_dump_json()),
        ttl_seconds=settings.ASSISTANT_CACHE_TTL_SECONDS,
    )
    return response


def _filters_dict(request: AssistantQueryRequest, db: Session, study_id, user_id) -> Optional[Dict[str, Any]]:
    if re.search(r"\boverall\b", request.message, flags=re.IGNORECASE):
        return None
    if request.filters is not None:
        data = request.filters.model_dump(exclude_none=True)
        return data or None
    if request.use_active_filters:
        active = get_active_filter(db, study_id, user_id)
        if active:
            return active
    return None


def run_assistant_query(
    db: Session,
    study_id,
    current_user: User,
    request: AssistantQueryRequest,
) -> AssistantQueryResponse:
    request_id = str(uuid.uuid4())
    if not _rate_limit_ok(str(current_user.id)):
        study_obj = authorize_study_for_assistant(db, study_id, current_user)
        return AssistantQueryResponse(
            request_id=request_id,
            status="error",
            answer_text="Rate limit exceeded. Please wait a minute and try again.",
            applied_context=build_applied_context(
                study_obj,
                {},
                AssistantQueryPlan(tool=AssistantToolName.unsupported),
                None,
            ),
            error="rate_limit",
            usage={"rate_limited": True},
        )

    try:
        study_obj = authorize_study_for_assistant(db, study_id, current_user)
    except AssistantToolError as exc:
        # Minimal error response without study context
        from app.schemas.assistant_schema import AppliedContext

        return AssistantQueryResponse(
            request_id=request_id,
            status="error",
            answer_text=exc.message,
            applied_context=AppliedContext(
                study_id=study_id,
                study_type="unknown",
                verified=False,
            ),
            error=exc.message,
        )

    # Private per-user conversation + durable user message (idempotent).
    conversation = get_or_create_conversation(
        db,
        study_id=study_obj.id,
        user_id=current_user.id,
    )
    client_message_id = request.client_message_id or str(uuid.uuid4())
    user_message = insert_user_message(
        db,
        conversation=conversation,
        content=request.message,
        client_message_id=client_message_id,
    )

    # Exact retry: if a successful assistant reply already exists, return it.
    # Failed replies are removed so the client can retry the same client_message_id.
    existing_reply = get_assistant_reply_for_parent(db, parent_message_id=user_message.id)
    if existing_reply:
        if existing_reply.status in {"error", "failed"}:
            db.delete(existing_reply)
            db.commit()
        else:
            cached_response = response_from_assistant_message(
                existing_reply,
                fallback_request_id=request_id,
            )
            if cached_response is not None:
                cached_response.conversation_id = conversation.id
                cached_response.usage = {
                    **(cached_response.usage or {}),
                    "history_replay": True,
                }
                return cached_response
            db.delete(existing_reply)
            db.commit()

    filters = _filters_dict(request, db, study_id, current_user.id)
    # The rendered answer bakes in whatever analysis settings were active when it
    # was computed (with/without intercept, thresholds, ...) — this must be part
    # of the key, or toggling a setting keeps serving the old answer text for up
    # to ASSISTANT_CACHE_TTL_SECONDS regardless of the new numbers.
    analysis_options_for_cache = get_study_analysis_settings(db, study_id, study=study_obj)
    cache_payload = {
        # Bumped for the tool-calling agent: cached template answers from the
        # single-tool planner must not be replayed over the new behaviour.
        "assistant_semantics_version": 22,
        "message": request.message,
        "filters": filters,
        "metric": request.metric.value if request.metric else None,
        "segment_section": request.segment_section,
        "segment_key": request.segment_key,
        "follow_up": request.follow_up.model_dump() if request.follow_up else None,
        "analysis_settings": analysis_options_for_cache,
    }
    cache_key = _cache_key(str(study_id), str(current_user.id), cache_payload)
    cached = RedisCache.get(cache_key)
    if isinstance(cached, dict) and cached.get("answer_text"):
        cached["request_id"] = request_id
        cached.setdefault("usage", {})["cache_hit"] = True
        try:
            response = AssistantQueryResponse(**cached)
            response.conversation_id = conversation.id
            response.user_message_id = user_message.id
            # Persist cache hit as a normal chat turn so history stays complete.
            assistant_row = insert_assistant_message(
                db,
                conversation=conversation,
                parent_message=user_message,
                content=response.answer_text,
                response=response,
                status="complete" if response.status != "error" else "error",
            )
            response.assistant_message_id = assistant_row.id
            return response
        except Exception:
            pass

    # The agent builds its study dictionary from the analysis, so its first call
    # cannot start until the analysis is ready — there is nothing to overlap.
    # On the fallback path the planner still runs concurrently with the prefetch
    # (network I/O releases the GIL). Only the main thread touches `db`, and
    # plan_query is pure network/regex, so that overlap stays safe.
    use_agent = bool(settings.ASSISTANT_AGENT_ENABLED) and not _is_simple_greeting(request.message)
    plan_future = None if use_agent else _PLANNER_POOL.submit(plan_query, request.message, request)

    prefetched_analysis: Optional[Dict[str, Any]] = None
    prefetch_error: Optional[Exception] = None
    try:
        prefetched_analysis = load_analysis_for_assistant(
            db, study_obj, current_user, filters=filters
        )
    except Exception as exc:
        logger.exception("Speculative analysis prefetch failed")
        prefetch_error = exc

    # A study with no completed responses has nothing to look up; the legacy
    # path already says so in one step, without spending tokens on it.
    if use_agent and not _has_responses(prefetched_analysis):
        use_agent = False

    agent_result: Optional[Dict[str, Any]] = None
    if use_agent and prefetched_analysis is not None:
        try:
            agent_result = run_agent_query(
                db,
                study_obj=study_obj,
                current_user=current_user,
                request=request,
                analysis=prefetched_analysis,
                filters=filters,
                conversation_id=conversation.id,
                user_message_id=user_message.id,
            )
        except AgentUnavailable as exc:
            logger.info("Assistant agent deferred to the planner: %s", exc)
        except Exception:
            logger.exception("Assistant agent failed; falling back to the planner")

    if agent_result is not None:
        return _finalize_agent_response(
            db,
            request_id=request_id,
            study_obj=study_obj,
            analysis=prefetched_analysis or {},
            filters=filters,
            agent_result=agent_result,
            conversation=conversation,
            user_message=user_message,
            cache_key=cache_key,
        )

    if plan_future is not None:
        try:
            plan, planner_usage = plan_future.result()
        except Exception as exc:
            logger.warning("Planner thread failed, using deterministic fallback: %s", exc)
            plan = _deterministic_plan(request.message, request)
            planner_usage = {"planner": "deterministic", "planner_error": str(exc)[:200]}
    else:
        # Agent path was tried and declined; plan inline.
        try:
            plan, planner_usage = plan_query(request.message, request)
            planner_usage = {**planner_usage, "agent_fallback": True}
        except Exception as exc:
            logger.warning("Inline planner failed, using deterministic fallback: %s", exc)
            plan = _deterministic_plan(request.message, request)
            planner_usage = {
                "planner": "deterministic",
                "planner_error": str(exc)[:200],
                "agent_fallback": True,
            }

    plan = _normalize_plan_for_question(
        plan,
        request.message,
        str(study_obj.study_type or "grid").lower(),
        request,
    )
    plan = _enrich_classification_plan_with_study(plan, request.message, study_obj, request)

    # Load analysis only when needed
    needs_analysis = plan.tool not in {
        AssistantToolName.greeting,
        AssistantToolName.clarify,
        AssistantToolName.unsupported,
        AssistantToolName.classification_distribution,
        AssistantToolName.list_saved_designs,
    }
    analysis: Dict[str, Any] = {}
    if needs_analysis or plan.tool == AssistantToolName.study_overview:
        try:
            # Reuse the concurrent prefetch when it succeeded; otherwise build now.
            if prefetched_analysis is not None:
                analysis = prefetched_analysis
            elif prefetch_error is not None:
                raise prefetch_error
            else:
                analysis = load_analysis_for_assistant(
                    db, study_obj, current_user, filters=filters
                )
        except Exception as exc:
            logger.exception("Failed to load analysis for assistant")
            context = build_applied_context(study_obj, {}, plan, filters)
            response = AssistantQueryResponse(
                request_id=request_id,
                status="error",
                answer_text="I could not load verified analysis for this study right now.",
                tool=plan.tool,
                applied_context=context,
                error=str(exc)[:200],
                usage=planner_usage,
                conversation_id=conversation.id,
                user_message_id=user_message.id,
            )
            assistant_row = insert_assistant_message(
                db,
                conversation=conversation,
                parent_message=user_message,
                content=response.answer_text,
                response=response,
                status="error",
            )
            response.assistant_message_id = assistant_row.id
            return response

    # Classification can still benefit from empty analysis context
    if plan.tool == AssistantToolName.classification_distribution and not analysis:
        if prefetched_analysis is not None:
            analysis = prefetched_analysis
        else:
            try:
                analysis = load_analysis_for_assistant(db, study_obj, current_user, filters=filters)
            except Exception:
                analysis = {}

    context = build_applied_context(study_obj, analysis, plan, filters)
    result = execute_tool(
        db=db,
        study_obj=study_obj,
        current_user=current_user,
        plan=plan,
        analysis=analysis,
        filters=filters,
        context=context,
        message=request.message,
    )

    status = result.get("status") or "answered"
    follow_up_ctx = result.get("follow_up_context")
    if isinstance(follow_up_ctx, dict):
        try:
            follow_up_model = AssistantFollowUpContext(**follow_up_ctx)
        except Exception:
            follow_up_model = None
    else:
        follow_up_model = None

    usage = {**planner_usage, **(result.get("usage") or {})}
    response = AssistantQueryResponse(
        request_id=request_id,
        status=status,
        answer_text=result.get("answer_text") or "",
        tool=plan.tool,
        applied_context=context,
        blocks=result.get("blocks") or [],
        evidence=result.get("evidence") or [],
        follow_ups=result.get("follow_ups") or [],
        actions=result.get("actions") or [],
        clarification_options=result.get("clarification_options") or plan.clarification_options or [],
        follow_up_context=follow_up_model,
        usage=usage,
        error=result.get("error"),
        conversation_id=conversation.id,
        user_message_id=user_message.id,
    )

    assistant_row = insert_assistant_message(
        db,
        conversation=conversation,
        parent_message=user_message,
        content=response.answer_text,
        response=response,
        status="complete" if status != "error" else "error",
    )
    response.assistant_message_id = assistant_row.id

    if status == "answered":
        RedisCache.set(cache_key, json.loads(response.model_dump_json()), ttl_seconds=settings.ASSISTANT_CACHE_TTL_SECONDS)
    return response
