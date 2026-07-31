"""
Dynamic tool-calling analytics agent.

The legacy planner mapped a question onto exactly one of a fixed set of tools and
answered with a Python string template, so anything outside those shapes fell
through to "clarify"/"unsupported". This module replaces that bottleneck with a
bounded tool-calling loop:

1. The model receives a compact dictionary of what this study actually contains
   (categories, element codes, segments, classification options, base sizes).
2. It may call several deterministic tools across a couple of rounds, so
   composite questions ("which element should I show the client, and does it
   hold for women?") resolve into multiple verified lookups.
3. It writes the final answer through the `respond` tool, restricted to numbers
   that appear in those tool results — every number is validated before the
   answer leaves this module.

All numbers still come from the existing deterministic tools. The model chooses
what to look up and how to phrase the answer; it never produces a figure.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.assistant_message_model import AssistantMessage
from app.models.study_model import Study
from app.models.user_model import User
from app.schemas.assistant_schema import (
    AssistantMetric,
    AssistantQueryPlan,
    AssistantQueryRequest,
    AssistantToolName,
    CompareMode,
    RankDirection,
)
from app.services.assistant_tools import (
    METRIC_LABELS,
    _available_segment_keys,
    _classification_questions,
    _extract_option_texts,
    build_applied_context,
    execute_tool,
    extract_age_segment_from_text,
    extract_gender_from_text,
    resolve_age_segment_key,
)
from app.services.design_optimizer import (
    build_categories_from_analysis,
    metric_prefix,
    section_key_for,
)

logger = logging.getLogger(__name__)


class AgentUnavailable(Exception):
    """Raised when the agent cannot run and the legacy planner should be used."""


# --------------------------------------------------------------------------- #
# Study dictionary
# --------------------------------------------------------------------------- #

# Caps keep the system prompt small — this is the only per-request cost that
# scales with study size.
_MAX_CATEGORIES = 14
_MAX_ELEMENTS_PER_CATEGORY = 14
_MAX_CLASSIFICATION_QUESTIONS = 12
_MAX_CLASSIFICATION_OPTIONS = 12
_NAME_TRUNCATE = 70


def _short(value: Any, limit: int = _NAME_TRUNCATE) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_study_dictionary(study_obj: Study, analysis: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compact catalog of what can actually be asked about this study.

    This is what makes arbitrary questions answerable: the model can only pick
    real segment keys, element codes, and classification options.
    """
    summary = analysis.get("dashboard_summary") or {}
    overall = analysis.get(section_key_for("T", "Overall")) or {}

    categories: List[Dict[str, Any]] = []
    for cat in (overall.get("categories") or [])[:_MAX_CATEGORIES]:
        elements = []
        for el in (cat.get("elements") or [])[:_MAX_ELEMENTS_PER_CATEGORY]:
            elements.append(f"{el.get('code')}: {_short(el.get('name'))}")
        categories.append(
            {
                "code": cat.get("code"),
                "name": _short(cat.get("name")),
                "elements": elements,
            }
        )

    # Read the keys from the same sheets the tools read, so the model can only
    # ever propose a segment the tools will actually accept.
    segment_keys = {
        section: _available_segment_keys(analysis, "T", section)[:20]
        for section in ("Gender", "Age", "Mindsets")
    }

    classification: List[Dict[str, Any]] = []
    for question in _classification_questions(study_obj)[:_MAX_CLASSIFICATION_QUESTIONS]:
        options = _extract_option_texts(question.answer_options)[:_MAX_CLASSIFICATION_OPTIONS]
        classification.append(
            {
                "question": _short(question.question_text, 140),
                "options": [_short(o, 90) for o in options],
            }
        )

    study_type = str(study_obj.study_type or "grid").lower()
    return {
        "study_title": _short(study_obj.title, 120),
        "study_type": study_type,
        "element_noun": "statement" if study_type == "text" else "element",
        "panelists": int(summary.get("uniquePanelists") or 0),
        "responses": int(summary.get("totalResponses") or 0),
        "avg_rating": round(float(summary.get("avgRating") or 0), 2),
        "avg_response_time_seconds": round(float(summary.get("avgResponseTime") or 0), 2),
        "base_size": overall.get("base_size"),
        "significance_threshold": overall.get("threshold"),
        "metrics": {
            "T": "Top Down — how strongly an element drives the top ratings (default)",
            "B": "Bottom Up — how strongly an element drives the bottom ratings",
            "R": "Response Time — how much an element slows or speeds response",
        },
        "categories": categories,
        "segments": segment_keys,
        "classification_questions": classification,
    }


# --------------------------------------------------------------------------- #
# Tool schemas exposed to the model
# --------------------------------------------------------------------------- #

_METRIC_ENUM = ["T", "B", "R"]
_DIRECTION_ENUM = ["highest", "lowest"]
_SECTION_ENUM = ["Overall", "Gender", "Age", "Mindsets"]


def _fn(name: str, description: str, properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }


_METRIC_PROP = {
    "type": "string",
    "enum": _METRIC_ENUM,
    "description": "Metric to use. Default T unless the user named another.",
}
_SEGMENT_SECTION_PROP = {
    "type": "string",
    "enum": _SECTION_ENUM,
    "description": "Segment family. Omit or use Overall for the whole sample.",
}
_SEGMENT_KEY_PROP = {
    "type": "string",
    "description": "Exact segment key from the study dictionary (e.g. Male, 25-34, Mindset 1).",
}

DATA_TOOLS: List[Dict[str, Any]] = [
    _fn(
        "rank_elements",
        "Rank individual elements/statements by their score. Use for questions about "
        "which single elements, claims, or images perform best or worst.",
        {
            "metric": _METRIC_PROP,
            "direction": {"type": "string", "enum": _DIRECTION_ENUM},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20,
                      "description": "1 for a singular ask, the stated count when given, else 5."},
            "segment_section": _SEGMENT_SECTION_PROP,
            "segment_key": _SEGMENT_KEY_PROP,
        },
    ),
    _fn(
        "rank_designs",
        "Rank complete designs (one element picked per category/layer) by total score. "
        "Use for best/worst design, mix, combination, package, or creative.",
        {
            "metric": _METRIC_PROP,
            "direction": {"type": "string", "enum": _DIRECTION_ENUM},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "segment_section": _SEGMENT_SECTION_PROP,
            "segment_key": _SEGMENT_KEY_PROP,
            "must_include": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Element codes or names the design must contain, copied "
                               "verbatim from the question. Never ranking words like 'best'.",
            },
            "classification_options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Restrict to respondents who chose these classification "
                               "answers. Must match options in the study dictionary.",
            },
        },
    ),
    _fn(
        "compare_two",
        "Side-by-side comparison of exactly two things: two segments, two ranked "
        "designs, best vs worst, or two classification cohorts.",
        {
            "mode": {"type": "string", "enum": ["segment", "design", "classification"]},
            "left": {"type": "string", "description": "First side, e.g. Male, 18-24, 'design 1', 'best'."},
            "right": {"type": "string", "description": "Second side, e.g. Female, 45-54, 'design 2', 'worst'."},
            "metric": _METRIC_PROP,
        },
        ["mode", "left", "right"],
    ),
    _fn(
        "compare_all_segments",
        "Compare every segment inside one family at once (all genders, all age bands, "
        "or all mindsets), including where they disagree most.",
        {
            "segment_section": {"type": "string", "enum": ["Gender", "Age", "Mindsets"]},
            "metric": _METRIC_PROP,
        },
        ["segment_section"],
    ),
    _fn(
        "lookup_element_scores",
        "Exact score for named elements, optionally within one segment. Use this for "
        "any question the ranking tools do not answer directly — a specific element's "
        "value, whether it beats the significance threshold, or how one element reads "
        "across segments (call once per segment).",
        {
            "metric": _METRIC_PROP,
            "elements": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Element codes (A1, B3) or names. Omit for every element.",
            },
            "segment_section": _SEGMENT_SECTION_PROP,
            "segment_key": _SEGMENT_KEY_PROP,
        },
    ),
    _fn(
        "segment_base_sizes",
        "How many respondents fall in each segment of a family. Use for 'how many "
        "men', sample-size questions, and checking a segment is large enough to trust.",
        {
            "metric": _METRIC_PROP,
            "segment_section": {"type": "string", "enum": ["Gender", "Age", "Mindsets", "Classification"]},
        },
        ["segment_section"],
    ),
    _fn(
        "classification_counts",
        "Counts and percentages of respondents per answer of a screening/classification "
        "question, optionally narrowed by gender and age.",
        {
            "question": {"type": "string", "description": "Question text from the study dictionary."},
            "options": {"type": "array", "items": {"type": "string"},
                        "description": "Specific answer options asked about."},
            "gender": {"type": "string", "description": "Male or Female."},
            "age": {"type": "string", "description": "Age bucket such as 25-34."},
        },
    ),
    _fn("study_overview", "Headline study stats: panelists, responses, average rating and response time.", {}),
    _fn("executive_summary", "Stakeholder-ready summary of the most important findings across the study.", {}),
    _fn(
        "use_or_avoid_elements",
        "Which elements to use and which to avoid, split by the significance threshold.",
        {"metric": _METRIC_PROP},
    ),
    _fn("response_time_summary", "How long respondents took and how that varied.", {}),
    _fn("fatigue_summary", "Whether respondent engagement decayed across tasks.", {}),
    _fn(
        "explain_mindset",
        "What defines a mindset segment and which elements drive it.",
        {"mindset_key": {"type": "string", "description": "Exact mindset key, e.g. 'Mindset 1'."},
         "metric": _METRIC_PROP},
    ),
    _fn(
        "explain_design",
        "Why the top-ranked design wins, element by element, and by how much it "
        "beats the runner-up. Only covers the #1 design — for any other design use "
        "compare_two in design mode.",
        {"metric": _METRIC_PROP},
    ),
    _fn("list_saved_designs", "Designs this team has saved for this study.", {}),
]

RESPOND_TOOL = _fn(
    "respond",
    "Deliver the final answer to the user. Call this exactly once, last.",
    {
        "answer": {
            "type": "string",
            "description": "The answer, written directly to the user. Lead with the "
                           "answer itself. Cite fact ids in square brackets like [E1] "
                           "for each number. Every number must appear in a tool result.",
        },
        "follow_up_questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Up to 3 short next questions the user is likely to want.",
        },
        "alternate_reading": {
            "type": "string",
            "description": "Only when the question had a second plausible reading: that "
                           "other reading phrased as a tappable question. Otherwise omit.",
        },
        "data_backed": {
            "type": "boolean",
            "description": "True when the answer rests on tool results.",
        },
    },
    ["answer"],
)

ALL_TOOLS = DATA_TOOLS + [RESPOND_TOOL]

_TOOL_TO_ASSISTANT_NAME = {
    "rank_elements": AssistantToolName.rank_elements,
    "rank_designs": AssistantToolName.rank_designs,
    "compare_two": AssistantToolName.compare,
    "compare_all_segments": AssistantToolName.compare_segments,
    "lookup_element_scores": AssistantToolName.rank_elements,
    "segment_base_sizes": AssistantToolName.study_overview,
    "classification_counts": AssistantToolName.classification_distribution,
    "study_overview": AssistantToolName.study_overview,
    "executive_summary": AssistantToolName.executive_summary,
    "use_or_avoid_elements": AssistantToolName.use_avoid_elements,
    "response_time_summary": AssistantToolName.response_time_summary,
    "fatigue_summary": AssistantToolName.fatigue_summary,
    "explain_mindset": AssistantToolName.explain_mindset,
    "explain_design": AssistantToolName.explain_design,
    "list_saved_designs": AssistantToolName.list_saved_designs,
}


# --------------------------------------------------------------------------- #
# System prompt
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """You are the analytics assistant for a single consumer-research study.
You answer questions about that study's verified results, for researchers who show
these answers to clients.

HOW YOU WORK
- Call tools to get facts, then call `respond` to answer. Never answer a data
  question from memory — you have no numbers of your own.
- You may call several tools before responding. Do that whenever the question
  needs more than one lookup (two segments, a value plus its context, a ranking
  plus a specific element).
- The study dictionary below lists the real categories, element codes, segment
  keys and classification options. Only ever pass values that appear there.

ANSWER THE QUESTION THAT WAS ASKED
- Lead with the direct answer in the first sentence. If asked which element to
  show a client, name it and say why. If asked whether something is true, say
  yes or no first. Do not open with a restatement of the question.
- Then give the supporting numbers, each with its fact id in brackets: [E1].
- Add the caveat only when it changes the decision — a small base size, a score
  below the significance threshold, a gap too narrow to matter.
- Be specific and brief: 2-5 sentences for a simple question. No headers, no
  bullet lists unless you are genuinely listing ranked items.

NUMBERS
- Every number in your answer must come from a tool result, verbatim. You may
  state the difference between two numbers you were given. Never estimate,
  extrapolate, round differently, or compute a percentage or ratio of your own —
  quote percentages only when a tool returned them.
- Scores are regression coefficients, not percentages. A score above the
  significance threshold matters; below it does not.

AMBIGUITY — ANSWER, DO NOT INTERROGATE
- Never ask the user what they meant when a reasonable reading exists. Pick the
  most likely reading, answer it fully, and put the other reading in
  `alternate_reading` so they can tap it.
- "best/top/strongest" with no noun means the best complete design. Add the
  elements reading as `alternate_reading`.
- A singular ask ("the best design") means limit 1. A plural ask with no number
  means 5. An explicit count always wins.
- If a tool reports it needs clarification, do not pass that back. Re-read the
  study dictionary, pick the closest real value, and call the tool again.

OFF-TOPIC
- For anything outside this study's data, say plainly that you only cover this
  study's analytics, name two things you can answer, and set data_backed false.
- For greetings, greet briefly and suggest two questions. No tools needed."""


# --------------------------------------------------------------------------- #
# Argument coercion
# --------------------------------------------------------------------------- #

def _metric(args: Dict[str, Any]) -> AssistantMetric:
    """Top Down unless the model explicitly asked for another metric."""
    raw = str(args.get("metric") or "").strip().upper()
    if raw in {"T", "B", "R"}:
        return AssistantMetric(raw)
    return AssistantMetric.T


def _direction(args: Dict[str, Any]) -> RankDirection:
    raw = str(args.get("direction") or "highest").strip().lower()
    return RankDirection.lowest if raw == "lowest" else RankDirection.highest


def _limit(args: Dict[str, Any], default: int = 5) -> int:
    try:
        value = int(args.get("limit") or default)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, settings.ASSISTANT_MAX_RESULT_LIMIT))


def _str_list(value: Any, cap: int = 8) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    for item in value:
        text = " ".join(str(item or "").strip().split())
        if text and text not in out:
            out.append(text[:120])
        if len(out) >= cap:
            break
    return out


def _resolve_segment_ref(
    section: Optional[str],
    key: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """Normalize a (section, key) pair, inferring the section from the key."""
    key_text = " ".join(str(key or "").strip().split())
    section_text = str(section or "").strip()

    if not key_text:
        return (None, None) if section_text.lower() in {"", "overall"} else (section_text, None)

    if re.search(r"mindset", key_text, flags=re.IGNORECASE):
        return "Mindsets", key_text

    gender = extract_gender_from_text(key_text)
    if gender:
        return "Gender", gender

    # resolve_age_segment_key handles bucket labels and bare numbers; the text
    # extractor handles prose like "22 years old".
    age = resolve_age_segment_key(key_text) or extract_age_segment_from_text(key_text)
    if age:
        return "Age", age

    if section_text and section_text.lower() != "overall":
        return section_text, key_text
    return None, None


def _plan_for(
    tool: AssistantToolName,
    *,
    metric: AssistantMetric,
    direction: RankDirection = RankDirection.highest,
    limit: int = 5,
    section: Optional[str] = None,
    key: Optional[str] = None,
    **extra: Any,
) -> AssistantQueryPlan:
    return AssistantQueryPlan(
        tool=tool,
        metric=metric,
        direction=direction,
        limit=limit,
        segment_section=section,
        segment_key=key,
        **extra,
    )


# --------------------------------------------------------------------------- #
# Primitive tools (not in the legacy tool set)
# --------------------------------------------------------------------------- #

def _tool_lookup_element_scores(
    analysis: Dict[str, Any],
    study_obj: Study,
    metric: AssistantMetric,
    requested: Sequence[str],
    section: Optional[str],
    key: Optional[str],
) -> Dict[str, Any]:
    """Exact verified score for named elements, in one segment."""
    prefix = metric_prefix(metric.value)
    categories = build_categories_from_analysis(
        analysis,
        metric=prefix,
        segment_section=section,
        segment_key=key,
        study_type=str(study_obj.study_type or "grid"),
    )
    flat = [el for cat in categories for el in (cat.elements or [])]
    if not flat:
        return {
            "status": "no_data",
            "answer_text": "No element coefficients are available for that segment yet.",
            "facts": [],
        }

    wanted = [w.casefold() for w in requested if w]
    if wanted:
        matched = []
        for el in flat:
            code = str(el.code or "").casefold()
            name = str(el.name or "").casefold()
            for w in wanted:
                if w == code or w == name or (len(w) > 3 and w in name):
                    matched.append(el)
                    break
        if not matched:
            available = ", ".join(f"{el.code} {el.name}" for el in flat[:20])
            return {
                "status": "not_found",
                "answer_text": f"No element matched. Available: {available}",
                "facts": [],
            }
    else:
        matched = flat[:40]

    section_label = key or "Overall"
    facts = []
    for idx, el in enumerate(matched[:40], start=1):
        facts.append(
            {
                "id": f"L{idx}",
                "label": f"{el.category_name}: {el.name} ({section_label})",
                "value": el.value,
                "code": el.code,
                "above_threshold": el.above_threshold,
            }
        )
    return {
        "status": "answered",
        "answer_text": (
            f"{len(facts)} element score(s) for {METRIC_LABELS.get(prefix, prefix)} "
            f"in {section_label}."
        ),
        "facts": facts,
    }


def _tool_segment_base_sizes(
    analysis: Dict[str, Any],
    metric: AssistantMetric,
    section: str,
) -> Dict[str, Any]:
    """Respondent counts per segment key, straight from the verified analysis."""
    prefix = metric_prefix(metric.value)
    overall = analysis.get(section_key_for(prefix, "Overall")) or {}
    normalized = str(section or "").strip().lower()

    source: Dict[str, Any] = {}
    if normalized == "classification":
        questions = (analysis.get(section_key_for(prefix, "Classification Questions")) or {}).get(
            "questions"
        ) or []
        for question in questions:
            label = _short(question.get("question_text"), 60)
            for answer, info in (question.get("segments") or {}).items():
                source[f"{label} — {_short(answer, 60)}"] = (info or {}).get("base_size")
    elif normalized in {"gender", "age", "mindsets"}:
        section_sheet = analysis.get(section_key_for(prefix, normalized.title())) or {}
        source = {
            key: (value or {}).get("base_size")
            for key, value in (section_sheet.get("segments") or {}).items()
        }
    if not source:
        source = {"Overall": overall.get("base_size")}

    facts: List[Dict[str, Any]] = []
    for idx, (label, base) in enumerate(list(source.items())[:30], start=1):
        facts.append({"id": f"S{idx}", "label": f"Base size — {label}", "value": base})

    if not facts:
        return {
            "status": "no_data",
            "answer_text": f"No {section} segments exist for this study.",
            "facts": [],
        }
    return {
        "status": "answered",
        "answer_text": f"{len(facts)} {section} segment base size(s).",
        "facts": facts,
        "total_base_size": overall.get("base_size"),
    }


# --------------------------------------------------------------------------- #
# Tool execution
# --------------------------------------------------------------------------- #

def _execute_agent_tool(
    name: str,
    args: Dict[str, Any],
    *,
    db: Session,
    study_obj: Study,
    current_user: User,
    analysis: Dict[str, Any],
    filters: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], AssistantQueryPlan]:
    """
    Run one model-requested tool through the existing verified implementations.

    Returns the raw tool result plus the plan it resolved to, which the caller
    uses to build the UI's metric/segment/base-size context chips.
    """
    metric = _metric(args)
    section, key = _resolve_segment_ref(args.get("segment_section"), args.get("segment_key"))

    if name == "lookup_element_scores":
        plan = _plan_for(AssistantToolName.rank_elements, metric=metric, section=section, key=key)
        return (
            _tool_lookup_element_scores(
                analysis, study_obj, metric, _str_list(args.get("elements"), cap=12), section, key
            ),
            plan,
        )
    if name == "segment_base_sizes":
        requested_section = str(args.get("segment_section") or "Gender")
        plan = _plan_for(AssistantToolName.study_overview, metric=metric)
        return _tool_segment_base_sizes(analysis, metric, requested_section), plan

    if name == "rank_elements":
        plan = _plan_for(
            AssistantToolName.rank_elements,
            metric=metric,
            direction=_direction(args),
            limit=_limit(args),
            section=section,
            key=key,
        )
    elif name == "rank_designs":
        plan = _plan_for(
            AssistantToolName.rank_designs,
            metric=metric,
            direction=_direction(args),
            limit=_limit(args, default=1),
            section=section,
            key=key,
            must_include=_str_list(args.get("must_include")),
            classification_options=_str_list(args.get("classification_options")),
        )
    elif name == "compare_two":
        raw_mode = str(args.get("mode") or "segment").strip().lower()
        mode = CompareMode(raw_mode) if raw_mode in {m.value for m in CompareMode} else CompareMode.segment
        plan = _plan_for(
            AssistantToolName.compare,
            metric=metric,
            limit=2,
            compare_mode=mode,
            compare_left=str(args.get("left") or "")[:120],
            compare_right=str(args.get("right") or "")[:120],
        )
    elif name == "compare_all_segments":
        plan = _plan_for(
            AssistantToolName.compare_segments,
            metric=metric,
            section=str(args.get("segment_section") or "Gender"),
        )
    elif name == "classification_counts":
        _, age_key = _resolve_segment_ref("Age", str(args.get("age") or ""))
        plan = _plan_for(
            AssistantToolName.classification_distribution,
            metric=metric,
            classification_question=(str(args.get("question")).strip()[:200] if args.get("question") else None),
            classification_options=_str_list(args.get("options")),
            gender_key=extract_gender_from_text(str(args.get("gender") or "")) or None,
            age_key=age_key,
        )
    elif name == "explain_mindset":
        plan = _plan_for(
            AssistantToolName.explain_mindset,
            metric=metric,
            section="Mindsets",
            key=str(args.get("mindset_key") or "Mindset 1")[:60],
        )
    elif name == "explain_design":
        plan = _plan_for(AssistantToolName.explain_design, metric=metric)
    elif name == "use_or_avoid_elements":
        plan = _plan_for(AssistantToolName.use_avoid_elements, metric=metric)
    elif name in {"study_overview", "executive_summary", "response_time_summary",
                  "fatigue_summary", "list_saved_designs"}:
        plan = _plan_for(_TOOL_TO_ASSISTANT_NAME[name], metric=metric)
    else:
        raise AgentUnavailable(f"unknown tool {name}")

    context = build_applied_context(study_obj, analysis, plan, filters)
    # Empty message: the model's explicit arguments are authoritative here, and
    # the legacy tools re-parse the raw question when one is supplied.
    result = execute_tool(
        db=db,
        study_obj=study_obj,
        current_user=current_user,
        plan=plan,
        analysis=analysis,
        filters=filters,
        context=context,
        message="",
    )
    return result, plan


# --------------------------------------------------------------------------- #
# Result compaction (what the model sees back)
# --------------------------------------------------------------------------- #

_MAX_FACTS_PER_TOOL = 25
_MAX_DESIGNS_PER_TOOL = 5


def _compact_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Strip images, transforms, and layout noise; keep labels and numbers."""
    compact: Dict[str, Any] = {"status": result.get("status") or "answered"}
    if result.get("answer_text"):
        compact["summary"] = _short(result["answer_text"], 400)

    facts: List[Dict[str, Any]] = []
    for fact in (result.get("facts") or [])[:_MAX_FACTS_PER_TOOL]:
        facts.append({k: v for k, v in fact.items() if v is not None})
    for fact in (result.get("evidence") or [])[:_MAX_FACTS_PER_TOOL]:
        entry = {"id": fact.get("fact_id"), "label": _short(fact.get("label"), 120)}
        if fact.get("value") is not None:
            entry["value"] = fact.get("value")
        meta = fact.get("meta") or {}
        if meta.get("code"):
            entry["code"] = meta["code"]
        facts.append(entry)
    if facts:
        compact["facts"] = facts[:_MAX_FACTS_PER_TOOL]

    for block in result.get("blocks") or []:
        btype = block.get("type")
        data = block.get("data") or {}
        if btype == "top_k_designs":
            designs = []
            for design in (data.get("designs") or [])[:_MAX_DESIGNS_PER_TOOL]:
                designs.append(
                    {
                        "rank": design.get("rank"),
                        "score": design.get("score"),
                        "fact_id": design.get("fact_id"),
                        "elements": [
                            f"{el.get('category_name')}: {_short(el.get('name'), 50)} ({el.get('value')})"
                            for el in (design.get("elements") or [])[:12]
                        ],
                    }
                )
            if designs:
                compact["designs"] = designs
        elif btype == "classification_distribution":
            options = [
                {"option": _short(o.get("option"), 90), "count": o.get("count"),
                 "percentage": o.get("percentage")}
                for o in (data.get("options") or [])[:15]
            ]
            if options:
                compact["classification"] = options
        elif btype == "side_by_side_compare":
            sides = []
            for side in (data.get("sides") or [])[:2]:
                top = side.get("top_design") or {}
                sides.append(
                    {
                        "label": _short(side.get("label"), 60),
                        "score": top.get("score") if top else side.get("score"),
                        "base_size": side.get("base_size"),
                        "top_element": _short((side.get("top_element") or {}).get("name"), 50) or None,
                    }
                )
            if sides:
                compact["sides"] = sides
        elif btype == "use_avoid":
            compact["use_avoid"] = {
                "use": [_short(x.get("name"), 50) for x in (data.get("use") or [])[:8]],
                "avoid": [_short(x.get("name"), 50) for x in (data.get("avoid") or [])[:8]],
            }

    if result.get("clarification_options"):
        compact["tool_wants_clarification_from_these"] = [
            _short(o, 90) for o in (result.get("clarification_options") or [])[:8]
        ]
    return compact


def _namespace_fact_ids(payload: Any, prefix: str) -> Any:
    """Prefix every fact_id in a tool result so ids stay unique across calls."""
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            if k == "fact_id" and isinstance(v, str):
                out[k] = f"{prefix}{v}"
            elif k == "id" and isinstance(v, str) and re.fullmatch(r"[A-Z]\d+", v):
                out[k] = f"{prefix}{v}"
            else:
                out[k] = _namespace_fact_ids(v, prefix)
        return out
    if isinstance(payload, list):
        return [_namespace_fact_ids(item, prefix) for item in payload]
    return payload


# --------------------------------------------------------------------------- #
# Numeric grounding
# --------------------------------------------------------------------------- #

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_MAX_GROUNDING_VALUES = 80


def _collect_numbers(payload: Any, verbatim: List[float], measured: List[float]) -> None:
    """
    Split the numbers in a tool payload into two pools.

    `verbatim` is everything the answer may quote, including figures baked into
    labels — an element literally named "Kills 99.9% of germs" has to be
    quotable. `measured` holds only true numeric values, and it alone seeds the
    difference derivation: a gap is only meaningful between two measurements.
    Letting a label's 99.9 into that pool made 99.9 - 13 vouch for a fabricated
    "87".
    """
    if len(verbatim) >= _MAX_GROUNDING_VALUES:
        return
    if isinstance(payload, bool):
        return
    if isinstance(payload, (int, float)):
        verbatim.append(float(payload))
        measured.append(float(payload))
        return
    if isinstance(payload, str):
        for match in _NUMBER_RE.findall(payload):
            try:
                verbatim.append(float(match))
            except ValueError:
                continue
            if len(verbatim) >= _MAX_GROUNDING_VALUES:
                return
        return
    if isinstance(payload, dict):
        for value in payload.values():
            _collect_numbers(value, verbatim, measured)
        return
    if isinstance(payload, (list, tuple)):
        for value in payload:
            _collect_numbers(value, verbatim, measured)


def _grounding_values(compacted: Iterable[Dict[str, Any]], question: str) -> List[float]:
    """
    Every number the answer is allowed to state, plus legitimate derivations.

    Only absolute gaps are derived. Ratios and percentages are deliberately not:
    deriving them makes the allowed set dense enough to admit almost any
    invented figure, which is exactly what this check exists to catch. The
    system prompt tells the model not to compute them, and any percentage that
    genuinely belongs to the data is already in the payload verbatim.
    """
    verbatim: List[float] = []
    measured: List[float] = []
    for entry in compacted:
        _collect_numbers(entry, verbatim, measured)
    _collect_numbers(question, verbatim, measured)
    # Ranks, counts and small ordinals are always quotable. This does mean a
    # fabricated score that happens to land in 0-20 slips through; rejecting
    # "ranks 3rd" or "the top 5" would be the worse trade. They stay out of the
    # difference pool below, though — subtracting a rank from a measurement is
    # never a real finding, and allowing it would vouch for things like 400 - 4.
    small_ordinals = {float(n) for n in range(0, 21)}

    measurements = sorted({round(v, 4) for v in measured})
    allowed = {round(v, 4) for v in verbatim} | set(measurements) | small_ordinals
    # A stated gap between two measured numbers is a legitimate answer.
    for i, left in enumerate(measurements):
        for right in measurements[i + 1:]:
            allowed.add(round(abs(left - right), 4))
    return sorted(allowed)


def _numbers_are_grounded(answer: str, allowed: Sequence[float]) -> Tuple[bool, Optional[str]]:
    if not answer:
        return True, None
    # Ignore bracketed fact citations like [E1] / [1.C3].
    stripped = re.sub(r"\[[^\]]*\]", " ", answer)
    for token in _NUMBER_RE.findall(stripped):
        try:
            value = float(token)
        except ValueError:
            continue
        # A tight absolute window plus explicit rounding forms. A proportional
        # tolerance would widen with the value and wave through big fabrications.
        if not any(
            abs(value - candidate) <= 0.02
            or round(candidate) == value
            or round(candidate, 1) == value
            or round(candidate, 2) == value
            for candidate in allowed
        ):
            return False, token
    return True, None


# --------------------------------------------------------------------------- #
# Conversation history
# --------------------------------------------------------------------------- #

_HISTORY_TURNS = 4
_HISTORY_CHARS = 300


def _recent_history(
    db: Session,
    *,
    conversation_id: Any,
    exclude_message_id: Any,
) -> List[Dict[str, str]]:
    """Last few turns, so 'show the opposite' and 'what about women' resolve."""
    try:
        rows = list(
            db.scalars(
                select(AssistantMessage)
                .where(AssistantMessage.conversation_id == conversation_id)
                .where(AssistantMessage.id != exclude_message_id)
                .order_by(AssistantMessage.created_at.desc(), AssistantMessage.id.desc())
                .limit(_HISTORY_TURNS * 2)
            ).all()
        )
    except Exception as exc:
        logger.debug("Agent history load failed: %s", exc)
        return []

    rows.reverse()
    history: List[Dict[str, str]] = []
    for row in rows:
        role = "assistant" if row.role == "assistant" else "user"
        content = _short(row.content, _HISTORY_CHARS)
        if content:
            history.append({"role": role, "content": content})
    return history[-(_HISTORY_TURNS * 2):]


# --------------------------------------------------------------------------- #
# Agent loop
# --------------------------------------------------------------------------- #

def _openai_client():
    from app.services.assistant_service import _openai_client as build_client

    return build_client()


def _tool_call_args(call: Any) -> Dict[str, Any]:
    try:
        parsed = json.loads(call.function.arguments or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, AttributeError):
        return {}


def run_agent_query(
    db: Session,
    *,
    study_obj: Study,
    current_user: User,
    request: AssistantQueryRequest,
    analysis: Dict[str, Any],
    filters: Optional[Dict[str, Any]],
    conversation_id: Any = None,
    user_message_id: Any = None,
) -> Dict[str, Any]:
    """
    Answer any question about this study by composing verified tool calls.

    Returns the same result shape the legacy `execute_tool` produces, so the
    response assembly in `assistant_service` is unchanged. Raises
    `AgentUnavailable` when the caller should fall back to the legacy planner.
    """
    client = _openai_client()
    if client is None:
        raise AgentUnavailable("OpenAI client unavailable")

    started = time.monotonic()
    budget = float(settings.ASSISTANT_AGENT_TOTAL_TIMEOUT_SECONDS)
    model = settings.ASSISTANT_AGENT_MODEL or settings.ASSISTANT_MODEL or "gpt-4o-mini"
    max_rounds = max(1, int(settings.ASSISTANT_AGENT_MAX_ROUNDS))
    max_calls = max(1, int(settings.ASSISTANT_AGENT_MAX_TOOL_CALLS))

    dictionary = build_study_dictionary(study_obj, analysis)
    messages: List[Dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                f"{SYSTEM_PROMPT}\n\nSTUDY DICTIONARY (the only valid values):\n"
                f"{json.dumps(dictionary, separators=(',', ':'), default=str)}"
            ),
        }
    ]
    if conversation_id is not None:
        messages.extend(
            _recent_history(db, conversation_id=conversation_id, exclude_message_id=user_message_id)
        )
    messages.append({"role": "user", "content": request.message})

    executed: List[Tuple[str, Dict[str, Any]]] = []   # (tool name, raw result)
    compacted: List[Dict[str, Any]] = []
    trace: List[Dict[str, Any]] = []
    prompt_tokens = 0
    completion_tokens = 0
    calls_made = 0
    final: Optional[Dict[str, Any]] = None
    plain_text: Optional[str] = None
    context_plan: Optional[AssistantQueryPlan] = None
    context_plan_is_fallback = False

    for round_index in range(max_rounds + 1):
        elapsed = time.monotonic() - started
        if elapsed > budget:
            logger.warning("Assistant agent exceeded its time budget after %.1fs", elapsed)
            break

        # The tool list stays identical every round so the cached prompt prefix
        # survives; the final round forces `respond` via tool_choice instead.
        last_round = round_index >= max_rounds or calls_made >= max_calls
        tool_choice = (
            {"type": "function", "function": {"name": "respond"}} if last_round else "auto"
        )

        try:
            response = client.chat.completions.create(
                model=model,
                temperature=0,
                max_tokens=settings.ASSISTANT_MAX_OUTPUT_TOKENS,
                messages=messages,
                tools=ALL_TOOLS,
                tool_choice=tool_choice,
                timeout=max(2.0, min(settings.ASSISTANT_TIMEOUT_SECONDS, budget - elapsed)),
            )
        except Exception as exc:
            logger.warning("Assistant agent LLM call failed on round %s: %s", round_index, exc)
            if not executed:
                raise AgentUnavailable(str(exc)) from exc
            break

        usage = getattr(response, "usage", None)
        prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens += getattr(usage, "completion_tokens", 0) or 0

        message = response.choices[0].message
        tool_calls = list(getattr(message, "tool_calls", None) or [])

        if not tool_calls:
            plain_text = (message.content or "").strip() or None
            break

        messages.append(
            {
                "role": "assistant",
                "content": message.content or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in tool_calls
                ],
            }
        )

        finished = False
        for call in tool_calls:
            name = call.function.name
            args = _tool_call_args(call)

            if name == "respond":
                final = args
                finished = True
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": '{"delivered":true}'}
                )
                break

            if calls_made >= max_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": '{"error":"tool budget exhausted, call respond now"}',
                    }
                )
                continue

            calls_made += 1
            try:
                raw, resolved_plan = _execute_agent_tool(
                    name,
                    args,
                    db=db,
                    study_obj=study_obj,
                    current_user=current_user,
                    analysis=analysis,
                    filters=filters,
                )
            except Exception as exc:
                logger.exception("Assistant agent tool %s failed", name)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps({"error": _short(str(exc), 200)}),
                    }
                )
                trace.append({"tool": name, "args": args, "error": _short(str(exc), 120)})
                continue

            executed.append((name, raw))
            # Context chips follow the first substantive tool; a bare lookup only
            # supplies them when nothing better ran.
            if context_plan is None or (context_plan_is_fallback and name not in _LOW_VALUE_TOOLS):
                context_plan = resolved_plan
                context_plan_is_fallback = name in _LOW_VALUE_TOOLS
            compact = _compact_result(raw)
            if len(executed) > 1:
                compact = _namespace_fact_ids(compact, f"{len(executed)}.")
            compacted.append(compact)
            trace.append({"tool": name, "args": args, "status": compact.get("status")})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(compact, separators=(",", ":"), default=str),
                }
            )

        if finished:
            break

    usage_meta = {
        "planner": "agent",
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tool_calls": calls_made,
        "trace": trace,
    }

    if final is None and plain_text is None:
        raise AgentUnavailable("agent produced no answer")

    assembled = _assemble_response(
        final=final,
        plain_text=plain_text,
        executed=executed,
        compacted=compacted,
        question=request.message,
        usage_meta=usage_meta,
    )
    assembled["agent_plan"] = context_plan
    return assembled


# --------------------------------------------------------------------------- #
# Response assembly
# --------------------------------------------------------------------------- #

_MAX_BLOCKS = 4
_MAX_FOLLOW_UPS = 4
# Tools whose payload is context rather than an answer in its own right.
_LOW_VALUE_TOOLS = {"lookup_element_scores", "segment_base_sizes"}


def _primary_result(executed: List[Tuple[str, Dict[str, Any]]]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """The tool whose blocks and actions should drive the UI."""
    for name, result in executed:
        if name not in _LOW_VALUE_TOOLS and (result.get("blocks") or result.get("evidence")):
            return name, result
    return executed[0] if executed else None


def _assemble_response(
    *,
    final: Optional[Dict[str, Any]],
    plain_text: Optional[str],
    executed: List[Tuple[str, Dict[str, Any]]],
    compacted: List[Dict[str, Any]],
    question: str,
    usage_meta: Dict[str, Any],
) -> Dict[str, Any]:
    answer = ""
    if isinstance(final, dict):
        answer = " ".join(str(final.get("answer") or "").split())
    if not answer:
        answer = " ".join((plain_text or "").split())

    primary = _primary_result(executed)
    primary_name, primary_result = primary if primary else (None, {})

    # Every number must trace back to a tool result. This runs even when no tool
    # ran: an answer with no data behind it must contain no data.
    grounding_fallback = False
    allowed = _grounding_values(compacted, question)
    ok, offender = _numbers_are_grounded(answer, allowed)
    if not ok:
        logger.warning(
            "Assistant agent answer rejected: %s is not grounded in tool results", offender
        )
        # A bare lookup's summary is not a usable answer, so in that case defer to
        # the planner instead of substituting it.
        fallback = (
            str(primary_result.get("answer_text") or "").strip()
            if primary_name and primary_name not in _LOW_VALUE_TOOLS
            else ""
        )
        if not fallback:
            raise AgentUnavailable(f"ungrounded number {offender} with no usable fallback")
        answer = fallback
        grounding_fallback = True
        usage_meta["grounding_rejected_value"] = offender

    if not answer:
        raise AgentUnavailable("empty answer")

    blocks: List[Dict[str, Any]] = []
    evidence: List[Dict[str, Any]] = []
    actions: List[Dict[str, Any]] = []
    tool_follow_ups: List[str] = []
    follow_up_context: Optional[Dict[str, Any]] = None
    seen_blocks: set = set()
    seen_actions: set = set()

    for index, (name, result) in enumerate(executed, start=1):
        namespaced = result if len(executed) == 1 else _namespace_fact_ids(result, f"{index}.")
        for block in namespaced.get("blocks") or []:
            marker = (block.get("type"), block.get("title"))
            if marker in seen_blocks:
                continue
            seen_blocks.add(marker)
            blocks.append(block)
        evidence.extend(namespaced.get("evidence") or [])
        for action in namespaced.get("actions") or []:
            marker = (action.get("type"), action.get("label"))
            if marker in seen_actions:
                continue
            seen_actions.add(marker)
            actions.append(action)
        tool_follow_ups.extend(namespaced.get("follow_ups") or [])
        if namespaced.get("follow_up_context"):
            follow_up_context = namespaced["follow_up_context"]

    follow_ups: List[str] = []
    if isinstance(final, dict):
        alternate = " ".join(str(final.get("alternate_reading") or "").split())
        if alternate:
            follow_ups.append(_short(alternate, 90))
        for item in _str_list(final.get("follow_up_questions"), cap=3):
            follow_ups.append(_short(item, 90))
    for item in tool_follow_ups:
        cleaned = _short(item, 90)
        if cleaned and cleaned not in follow_ups:
            follow_ups.append(cleaned)

    data_backed = bool(executed)
    if isinstance(final, dict) and final.get("data_backed") is False:
        data_backed = False

    usage_meta["grounding_fallback"] = grounding_fallback
    usage_meta["data_backed"] = data_backed

    return {
        "status": "answered",
        "answer_text": answer,
        "blocks": blocks[:_MAX_BLOCKS],
        "evidence": evidence,
        "follow_ups": follow_ups[:_MAX_FOLLOW_UPS],
        "actions": actions[:4],
        "clarification_options": [],
        "follow_up_context": follow_up_context,
        "usage": usage_meta,
        "agent_primary_tool": _TOOL_TO_ASSISTANT_NAME.get(primary_name or "") if primary_name else None,
        "agent_data_backed": data_backed,
    }
