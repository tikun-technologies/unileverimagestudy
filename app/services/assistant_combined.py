"""
T Combined as a verified lookup table for the analytics assistant.

The Excel "(T) Combined" sheet (and B/R) is the wide coefficient matrix:
every element × Overall / Gender / Age / classification. Numbers here are
computed by StudyAnalysisService — the model never invents them.

If the Combined sheet is missing, we rebuild the same matrix from the
Overall / Gender / Age / Classification sheets so the assistant still works.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.config import settings
from app.models.study_model import Study
from app.schemas.assistant_schema import (
    AssistantBlock,
    AssistantMetric,
    AssistantQueryPlan,
    DesignElementSnapshot,
    DesignRankItem,
    ElementRankItem,
    EvidenceFact,
    RankDirection,
)
from app.services.assistant_tools import (
    METRIC_LABELS,
    enrich_categories_from_study_layers,
    extract_age_segment_from_text,
    extract_gender_from_text,
    resolve_age_segment_key,
)
from app.services.design_optimizer import (
    OptimizerElement,
    build_categories_from_analysis,
    metric_prefix,
)

# Luna's context is ~1M tokens. Combined is one Excel sheet — send all
# Overall / Gender / Age / classification columns, do not drop them.
_MAX_PROMPT_CHARS = 400000
_MAX_RESULT_ROWS = 50
_MAX_FACTS = 40


def combined_sheet_key(metric: str) -> str:
    prefix = metric_prefix(metric)
    return f"({prefix}) Combined"


def compact_combined(
    analysis: Dict[str, Any],
    metric: str = "T",
    *,
    for_prompt: bool = False,
) -> Dict[str, Any]:
    """
    The export CSV/XLSX sheet ``(T) Combined`` (or B/R).

    One row per element, one column per segment: Overall, Male, Female,
    every age bin, and every classification question × answer. That is the
    table we give Luna — not the Overall-only sheet.
    """
    prefix = metric_prefix(metric)
    sheet = analysis.get(combined_sheet_key(prefix)) or {}
    if sheet.get("categories"):
        table = _from_combined_sheet(sheet, prefix)
    else:
        table = _from_segment_sheets(analysis, prefix)
    if for_prompt:
        return _prompt_table(table)
    return table


def combined_prompt_json(analysis: Dict[str, Any], metric: str = "T") -> str:
    """Full Combined sheet as JSON for the system prompt (prompt-cache friendly)."""
    import json

    table = compact_combined(analysis, metric, for_prompt=True)
    payload = json.dumps(table, separators=(",", ":"), default=str)
    if len(payload) > _MAX_PROMPT_CHARS:
        table["truncated"] = True
        for el in table.get("elements") or []:
            el["name"] = _short(el.get("name"), 60)
        payload = json.dumps(table, separators=(",", ":"), default=str)
    return payload


def query_combined(
    analysis: Dict[str, Any],
    study_obj: Study,
    args: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Deterministic slice of Combined. All numbers come from the table.

    ops:
      rank            — top/bottom N on one column
      lookup          — named elements across one or more columns
      compare_columns — two columns side by side
      best_segment    — which Combined column (segment) is strongest
      compose_design  — stack named elements (one per category) with images
    """
    metric = metric_prefix(str(args.get("metric") or "T"))
    table = compact_combined(analysis, metric)
    elements = list(table.get("elements") or [])
    columns = list(table.get("columns") or [])
    if not elements:
        return {
            "status": "answered",
            "answer_text": "This study does not have a Combined coefficient table yet. Complete responses first.",
            "blocks": [],
            "evidence": [],
            "follow_ups": ["Show study overview"],
            "facts": [],
        }

    op = str(args.get("op") or "rank").strip().lower()
    if op in {"best_column", "best_columns", "strongest_segment"}:
        op = "best_segment"
    if op in {"design", "pack", "mix", "stack"}:
        op = "compose_design"

    catalog = _image_catalog(analysis, study_obj, metric)

    if op == "lookup":
        return _op_lookup(table, args, catalog, study_obj)
    if op == "compare_columns":
        return _op_compare_columns(table, args, catalog, study_obj)
    if op == "best_segment":
        return _op_best_segment(table, args, catalog, study_obj)
    if op == "compose_design":
        return _op_compose_design(table, args, catalog, study_obj, analysis)
    return _op_rank(table, args, catalog, study_obj)


def execute_combined_plan(
    analysis: Dict[str, Any],
    study_obj: Study,
    plan: AssistantQueryPlan,
    message: str = "",
) -> Dict[str, Any]:
    """Planner-path entry: treat an unmatched question as a Combined rank/lookup."""
    args: Dict[str, Any] = {
        "metric": (plan.metric or AssistantMetric.T).value,
        "direction": (plan.direction or RankDirection.highest).value,
        "limit": plan.limit or 5,
        "column": plan.segment_key or plan.segment_section or "Overall",
        "op": "rank",
        "elements": list(plan.must_include or []),
    }
    text = (message or "").casefold()
    if any(tok in text for tok in ("segment", "which group", "which audience", "which cohort")):
        args["op"] = "best_segment"
    if plan.must_include and any(tok in text for tok in ("design", "mix", "pack", "combination", "stack")):
        args["op"] = "compose_design"
        args["elements"] = list(plan.must_include)
    return query_combined(analysis, study_obj, args)


# --------------------------------------------------------------------------- #
# Table builders
# --------------------------------------------------------------------------- #

def _from_combined_sheet(sheet: Dict[str, Any], metric: str) -> Dict[str, Any]:
    """Mirror the Excel Combined sheet: every value key becomes a column."""
    base_by_key: Dict[str, Any] = {}
    segs = sheet.get("segments") or {}
    base_by_key["Overall"] = (segs.get("Overall") or {}).get("base_size") or sheet.get("base_size")
    for name, info in (segs.get("Gender") or {}).items():
        base_by_key[f"Gender::{name}"] = (info or {}).get("base_size")
    for name, info in (segs.get("Age") or {}).items():
        base_by_key[f"Age::{name}"] = (info or {}).get("base_size")
    for question, info in (segs.get("Classification") or {}).items():
        for ans, ainfo in ((info or {}).get("answers") or {}).items():
            base_by_key[f"Classification::{question}::{ans}"] = (ainfo or {}).get("base_size")

    ordered_keys: List[str] = []
    seen = set()

    def remember(key: str) -> None:
        if key in seen:
            return
        seen.add(key)
        ordered_keys.append(key)

    # Excel column order: Overall, gender, age, then classification in sheet order.
    remember("Overall")
    for name in (segs.get("Gender") or {}):
        remember(f"Gender::{name}")
    for name in (segs.get("Age") or {}):
        remember(f"Age::{name}")
    for question, info in (segs.get("Classification") or {}).items():
        for ans in (info or {}).get("answers") or {}:
            remember(f"Classification::{question}::{ans}")
    for cat in sheet.get("categories") or []:
        for el in cat.get("elements") or []:
            for key in (el.get("values") or {}).keys():
                remember(str(key))

    columns = [
        {
            "key": key,
            "label": _label_for_combined_key(key),
            "family": _family_for_combined_key(key),
            "base_size": _int_or_none(base_by_key.get(key)),
        }
        for key in ordered_keys
    ]

    out_elements = []
    for cat in sheet.get("categories") or []:
        for el in cat.get("elements") or []:
            values = {str(k): _num(v) for k, v in (el.get("values") or {}).items()}
            flags = {str(k): bool(v) for k, v in (el.get("above_threshold") or {}).items()}
            out_elements.append(
                {
                    "code": el.get("code"),
                    "name": el.get("name"),
                    "category": cat.get("name"),
                    "category_code": cat.get("code"),
                    "values": values,
                    "above_threshold": flags,
                }
            )
    return {
        "sheet": f"({metric}) Combined",
        "metric": metric,
        "metric_label": METRIC_LABELS.get(metric, metric),
        "threshold": sheet.get("threshold"),
        "base_size": _int_or_none(sheet.get("base_size")),
        "columns": columns,
        "elements": out_elements,
        "source": "combined_sheet",
    }


def _from_segment_sheets(analysis: Dict[str, Any], metric: str) -> Dict[str, Any]:
    overall = analysis.get(f"({metric}) Overall") or {}
    gender = analysis.get(f"({metric}) Gender") or {}
    age = analysis.get(f"({metric}) Age") or {}
    classification = analysis.get(f"({metric}) Classification Questions") or {}

    columns: List[Dict[str, Any]] = [
        {"key": "Overall", "label": "Overall", "base_size": _int_or_none(overall.get("base_size"))}
    ]
    for name, info in (gender.get("segments") or {}).items():
        columns.append({"key": f"Gender::{name}", "label": str(name), "base_size": _int_or_none((info or {}).get("base_size"))})
    for name, info in (age.get("segments") or {}).items():
        columns.append({"key": f"Age::{name}", "label": str(name), "base_size": _int_or_none((info or {}).get("base_size"))})
    for question in classification.get("questions") or []:
        qtext = question.get("question_text") or ""
        for ans, info in (question.get("segments") or {}).items():
            columns.append(
                {
                    "key": f"Classification::{qtext}::{ans}",
                    "label": f"{_short(qtext, 40)} — {_short(ans, 40)}",
                    "base_size": _int_or_none((info or {}).get("base_size")),
                }
            )

    index: Dict[str, Dict[str, Any]] = {}

    def upsert(cat_name, cat_code, el, column_key, value, above) -> None:
        code = str(el.get("code") or "")
        name = str(el.get("name") or "")
        key = code or name
        row = index.setdefault(
            key,
            {
                "code": code or None,
                "name": name,
                "category": cat_name,
                "category_code": cat_code,
                "values": {},
                "above_threshold": {},
            },
        )
        row["values"][column_key] = _num(value)
        row["above_threshold"][column_key] = bool(above)

    for cat in overall.get("categories") or []:
        for el in cat.get("elements") or []:
            upsert(cat.get("name"), cat.get("code"), el, "Overall", el.get("value"), el.get("above_threshold"))

    def absorb_segment_sheet(sheet: Dict[str, Any], family: str) -> None:
        cats = sheet.get("categories") or []
        for cat in cats:
            for el in cat.get("elements") or []:
                values = el.get("values") or {}
                flags = el.get("above_threshold") or {}
                if values:
                    for seg, val in values.items():
                        col = f"{family}::{seg}" if family else str(seg)
                        flag = flags.get(seg) if isinstance(flags, dict) else flags
                        upsert(cat.get("name"), cat.get("code"), el, col, val, flag)
                elif el.get("value") is not None:
                    upsert(cat.get("name"), cat.get("code"), el, family, el.get("value"), el.get("above_threshold"))

    absorb_segment_sheet(gender, "Gender")
    absorb_segment_sheet(age, "Age")
    for question in classification.get("questions") or []:
        qtext = question.get("question_text") or ""
        for cat in question.get("categories") or []:
            for el in cat.get("elements") or []:
                values = el.get("values") or {}
                flags = el.get("above_threshold") or {}
                for ans, val in values.items():
                    col = f"Classification::{qtext}::{ans}"
                    flag = flags.get(ans) if isinstance(flags, dict) else flags
                    upsert(cat.get("name"), cat.get("code"), el, col, val, flag)

    return {
        "sheet": f"({metric}) Combined",
        "metric": metric,
        "metric_label": METRIC_LABELS.get(metric, metric),
        "threshold": overall.get("threshold"),
        "base_size": _int_or_none(overall.get("base_size")),
        "columns": columns,
        "elements": list(index.values()),
        "source": "synthesized",
    }


def _prompt_table(table: Dict[str, Any]) -> Dict[str, Any]:
    """Keep every Combined column (gender, age, classification). Do not slim the sheet."""
    return {
        "sheet": table.get("sheet") or "(T) Combined",
        "metric": table.get("metric"),
        "metric_label": table.get("metric_label"),
        "threshold": table.get("threshold"),
        "base_size": table.get("base_size"),
        "columns": list(table.get("columns") or []),
        "elements": [
            {
                "code": el.get("code"),
                "name": el.get("name"),
                "category": el.get("category"),
                "values": dict(el.get("values") or {}),
            }
            for el in (table.get("elements") or [])
        ],
        "source": table.get("source"),
    }


# --------------------------------------------------------------------------- #
# Ops
# --------------------------------------------------------------------------- #

def _op_rank(
    table: Dict[str, Any],
    args: Dict[str, Any],
    catalog: Dict[str, OptimizerElement],
    study_obj: Study,
) -> Dict[str, Any]:
    column = _resolve_column(table.get("columns") or [], args.get("column") or args.get("segment") or "Overall")
    if column is None:
        return _unknown_column(table, args.get("column"))
    direction = str(args.get("direction") or "highest").lower()
    reverse = direction != "lowest"
    limit = _limit(args)
    wanted = _str_list(args.get("elements"))
    rows = list(table.get("elements") or [])
    if wanted:
        rows = [el for el in rows if _element_matches(el, wanted)]
        if not rows:
            return _not_found(wanted, table)
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for el in rows:
        val = (el.get("values") or {}).get(column)
        if val is None:
            continue
        scored.append((_num(val), el))
    scored.sort(key=lambda item: (-item[0] if reverse else item[0], str(item[1].get("code") or ""), str(item[1].get("name") or "")))
    scored = scored[:limit]
    return _elements_result(
        table,
        scored,
        catalog,
        study_obj,
        title=f"{'Highest' if reverse else 'Lowest'} on { _column_label(table, column) }",
        summary_verb="highest" if reverse else "lowest",
        column=column,
    )


def _op_lookup(
    table: Dict[str, Any],
    args: Dict[str, Any],
    catalog: Dict[str, OptimizerElement],
    study_obj: Study,
) -> Dict[str, Any]:
    wanted = _str_list(args.get("elements"))
    if not wanted:
        return _op_rank(table, {**args, "limit": min(_limit(args), 8)}, catalog, study_obj)
    column_hints = _str_list(args.get("columns") or args.get("column"))
    columns = []
    if column_hints:
        for hint in column_hints:
            resolved = _resolve_column(table.get("columns") or [], hint)
            if resolved:
                columns.append(resolved)
    if not columns:
        columns = ["Overall"]
    matched = [el for el in (table.get("elements") or []) if _element_matches(el, wanted)]
    if not matched:
        return _not_found(wanted, table)
    facts = []
    items = []
    idx = 0
    for el in matched:
        for col in columns:
            idx += 1
            val = (el.get("values") or {}).get(col)
            if val is None:
                continue
            fid = f"C{idx}"
            facts.append(
                {
                    "id": fid,
                    "label": f"{el.get('name')} · {_column_label(table, col)}",
                    "value": _num(val),
                    "code": el.get("code"),
                }
            )
        items.append(_rank_item(len(items) + 1, el, catalog, (el.get("values") or {}).get(columns[0]) or 0, f"C{len(items)+1}"))
    names = ", ".join(f"“{el.get('name')}”" for el in matched[:4])
    col_label = ", ".join(_column_label(table, c) for c in columns)
    return {
        "status": "answered",
        "answer_text": f"{names} on {col_label} ({table.get('metric_label')}).",
        "blocks": [
            AssistantBlock(
                type="top_bottom_elements",
                title=f"Combined lookup · {col_label}",
                data={
                    "direction": "highest",
                    "metric": table.get("metric_label"),
                    "items": [i.model_dump() for i in items],
                },
            ).model_dump()
        ],
        "evidence": [_evidence_from_fact(f) for f in facts],
        "facts": facts[:_MAX_FACTS],
        "follow_ups": ["Compare these across gender", "Show the best design using these"],
        "actions": [],
    }


def _op_compare_columns(
    table: Dict[str, Any],
    args: Dict[str, Any],
    catalog: Dict[str, OptimizerElement],
    study_obj: Study,
) -> Dict[str, Any]:
    left = _resolve_column(table.get("columns") or [], args.get("left") or args.get("column"))
    right = _resolve_column(table.get("columns") or [], args.get("right") or args.get("compare_to"))
    if not left or not right:
        return _unknown_column(table, args.get("left") or args.get("right"))
    limit = _limit(args, default=8)
    rows = []
    for el in table.get("elements") or []:
        lv = (el.get("values") or {}).get(left)
        rv = (el.get("values") or {}).get(right)
        if lv is None or rv is None:
            continue
        gap = _num(lv) - _num(rv)
        rows.append((abs(gap), gap, el, _num(lv), _num(rv)))
    rows.sort(key=lambda item: (-item[0], str(item[2].get("code") or "")))
    rows = rows[:limit]
    facts = []
    for idx, (agap, gap, el, lv, rv) in enumerate(rows, start=1):
        facts.append(
            {
                "id": f"C{idx}",
                "label": f"{el.get('name')} · {_column_label(table, left)} vs {_column_label(table, right)}",
                "value": round(gap, 4),
                "code": el.get("code"),
                "left": lv,
                "right": rv,
            }
        )
    if not rows:
        return {
            "status": "answered",
            "answer_text": f"No overlapping Combined scores for {_column_label(table, left)} vs {_column_label(table, right)}.",
            "blocks": [],
            "evidence": [],
            "facts": [],
            "follow_ups": ["Show Combined overall ranking"],
            "actions": [],
        }
    top = rows[0]
    winner_side = _column_label(table, left) if top[1] > 0 else _column_label(table, right)
    items = [
        _rank_item(i, el, catalog, lv if abs(lv) >= abs(rv) else rv, f"C{i}")
        for i, (_a, _g, el, lv, rv) in enumerate(rows, start=1)
    ]
    return {
        "status": "answered",
        "answer_text": (
            f"The biggest Combined gap between {_column_label(table, left)} and "
            f"{_column_label(table, right)} is “{top[2].get('name')}” "
            f"({_num(top[3])} vs {_num(top[4])}, gap {round(abs(top[1]), 2)}) [{facts[0]['id']}]. "
            f"{winner_side} is stronger on that element."
        ),
        "blocks": [
            AssistantBlock(
                type="top_bottom_elements",
                title=f"{_column_label(table, left)} vs {_column_label(table, right)}",
                data={"direction": "highest", "metric": table.get("metric_label"), "items": [i.model_dump() for i in items]},
            ).model_dump()
        ],
        "evidence": [_evidence_from_fact(f) for f in facts],
        "facts": facts,
        "follow_ups": ["Show the best element overall", f"Rank {_column_label(table, left)} only"],
        "actions": [],
    }


def _op_best_segment(
    table: Dict[str, Any],
    args: Dict[str, Any],
    catalog: Dict[str, OptimizerElement],
    study_obj: Study,
) -> Dict[str, Any]:
    """Which Combined column (segment) has the strongest top coefficient."""
    columns = [c for c in (table.get("columns") or []) if c.get("key") != "Overall"]
    if not columns:
        return _op_rank(table, {**args, "column": "Overall"}, catalog, study_obj)
    wanted = _str_list(args.get("elements"))
    scored_cols: List[Tuple[float, str, Dict[str, Any]]] = []
    for col in columns:
        key = col["key"]
        best: Optional[Tuple[float, Dict[str, Any]]] = None
        for el in table.get("elements") or []:
            if wanted and not _element_matches(el, wanted):
                continue
            val = (el.get("values") or {}).get(key)
            if val is None:
                continue
            num = _num(val)
            if best is None or num > best[0]:
                best = (num, el)
        if best:
            scored_cols.append((best[0], key, best[1]))
    if not scored_cols:
        return _op_rank(table, args, catalog, study_obj)
    scored_cols.sort(key=lambda item: (-item[0], item[1]))
    top_n = scored_cols[: min(8, _limit(args, default=5))]
    facts = []
    for idx, (val, key, el) in enumerate(top_n, start=1):
        facts.append(
            {
                "id": f"S{idx}",
                "label": f"{_column_label(table, key)} · {el.get('name')}",
                "value": val,
                "code": el.get("code"),
                "column": key,
            }
        )
    winner_val, winner_key, winner_el = scored_cols[0]
    rows = [
        {
            "segment": _column_label(table, key),
            "avg": val,
            "top": val,
            "count": 1,
            "fact_id": f"S{idx}",
            "element": el.get("name"),
            "code": el.get("code"),
        }
        for idx, (val, key, el) in enumerate(top_n, start=1)
    ]
    items = [_rank_item(1, winner_el, catalog, winner_val, "S1")]
    listed = "; ".join(_column_label(table, key) for _val, key, _el in top_n)
    return {
        "status": "answered",
        "answer_text": (
            f"Top {len(top_n)} Combined segments: {listed}. "
            f"The lead is {_column_label(table, winner_key)} "
            f"on “{winner_el.get('name')}” [S1]."
        ),
        "blocks": [
            AssistantBlock(
                type="segment_comparison",
                title="Combined segment comparison",
                data={"rows": rows, "metric": table.get("metric_label")},
            ).model_dump(),
            AssistantBlock(
                type="top_bottom_elements",
                title="Strongest Combined segments",
                data={
                    "direction": "highest",
                    "metric": table.get("metric_label"),
                    "items": [i.model_dump() for i in items],
                },
            ).model_dump(),
        ],
        "evidence": [_evidence_from_fact(f) for f in facts],
        "facts": facts,
        "follow_ups": [
            f"Show top elements for {_column_label(table, winner_key)}",
            "Rank Overall instead",
            "Compare these segments by gender instead",
        ],
        "actions": [],
    }


def _op_compose_design(
    table: Dict[str, Any],
    args: Dict[str, Any],
    catalog: Dict[str, OptimizerElement],
    study_obj: Study,
    analysis: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a stacked pack from element codes/names (one per category)."""
    column = _resolve_column(table.get("columns") or [], args.get("column") or "Overall") or "Overall"
    codes = _str_list(args.get("elements") or args.get("codes"))
    if not codes:
        # Best element per category on this column → a full pack.
        by_cat: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        for el in table.get("elements") or []:
            cat = str(el.get("category") or "Layer")
            val = (el.get("values") or {}).get(column)
            if val is None:
                continue
            num = _num(val)
            prev = by_cat.get(cat)
            if prev is None or num > prev[0]:
                by_cat[cat] = (num, el)
        chosen = [pair[1] for pair in by_cat.values()]
    else:
        chosen = []
        used_cats = set()
        for hint in codes:
            match = next((el for el in (table.get("elements") or []) if _element_matches(el, [hint])), None)
            if not match:
                continue
            cat = str(match.get("category") or "")
            if cat in used_cats:
                continue
            used_cats.add(cat)
            chosen.append(match)
    if not chosen:
        return _not_found(codes or ["(best per layer)"], table)

    snapshots: List[DesignElementSnapshot] = []
    total = 0.0
    facts = []
    selected: Dict[str, str] = {}
    for idx, el in enumerate(chosen, start=1):
        val = _num((el.get("values") or {}).get(column) or 0)
        total += val
        opt = _catalog_match(el, catalog)
        snap = DesignElementSnapshot(
            element_id=(opt.element_id if opt else str(el.get("code") or el.get("name"))),
            category_key=(opt.category_key if opt else str(el.get("category_code") or el.get("category") or f"L{idx}")),
            category_name=str(el.get("category") or (opt.category_name if opt else "Layer")),
            name=str(el.get("name") or ""),
            value=val,
            code=el.get("code"),
            image_url=opt.image_url if opt else None,
            element_type=(opt.element_type if opt else None) or _element_type_for_study(study_obj),
            z_index=opt.z_index if opt else idx,
            layer_id=opt.layer_id if opt else None,
            image_id=opt.image_id if opt else None,
            transform=opt.transform if opt else None,
        )
        snapshots.append(snap)
        selected[snap.category_key] = snap.element_id
        facts.append({"id": f"D1.E{idx}", "label": f"{snap.category_name}: {snap.name}", "value": val, "code": el.get("code")})
    snapshots.sort(key=lambda s: (s.z_index, s.category_name.lower(), s.name.lower()))
    facts.append({"id": "D1", "label": "Combined pack score", "value": round(total, 4)})
    design = DesignRankItem(
        rank=1,
        score=round(total, 4),
        selection_count=len(snapshots),
        selected_by_category=selected,
        elements=snapshots,
        fact_id="D1",
        constraints_applied=False,
        complete_layers=True,
    )
    info = (analysis or {}).get("Information Block") or {}
    study_type = str(getattr(study_obj, "study_type", "") or "grid").lower()
    names = " + ".join(s.name for s in snapshots)
    return {
        "status": "answered",
        "answer_text": (
            f"This pack scores {round(total, 2)} on {_column_label(table, column)} "
            f"({table.get('metric_label')}): {names} [D1]."
        ),
        "blocks": [
            AssistantBlock(
                type="top_k_designs",
                title="Combined pack",
                data={
                    "direction": "highest",
                    "metric": table.get("metric_label"),
                    "study_type": study_type,
                    "background_url": info.get("Study Background") or info.get("background_image_url"),
                    "aspect_ratio": info.get("Aspect Ratio") or "9 / 16",
                    "designs": [design.model_dump()],
                },
            ).model_dump()
        ],
        "evidence": [_evidence_from_fact(f) for f in facts],
        "facts": facts,
        "follow_ups": ["Show the best verified design instead", "Rank elements overall"],
        "actions": [],
    }


def _elements_result(
    table: Dict[str, Any],
    scored: Sequence[Tuple[float, Dict[str, Any]]],
    catalog: Dict[str, OptimizerElement],
    study_obj: Study,
    *,
    title: str,
    summary_verb: str,
    column: str,
) -> Dict[str, Any]:
    if not scored:
        return {
            "status": "answered",
            "answer_text": f"No Combined scores are available for {_column_label(table, column)}.",
            "blocks": [],
            "evidence": [],
            "facts": [],
            "follow_ups": ["Show study overview"],
            "actions": [],
        }
    items = []
    facts = []
    for idx, (val, el) in enumerate(scored, start=1):
        fid = f"C{idx}"
        items.append(_rank_item(idx, el, catalog, val, fid))
        facts.append(
            {
                "id": fid,
                "label": f"{el.get('category')}: {el.get('name')}",
                "value": val,
                "code": el.get("code"),
            }
        )
    study_type = str(getattr(study_obj, "study_type", "") or "grid").lower()
    noun = "statement" if study_type == "text" else "element"
    top = items[0]
    if len(items) == 1:
        answer = (
            f"The {summary_verb}-scoring {noun} on {_column_label(table, column)} "
            f"({table.get('metric_label')}) is “{top.name}” at {top.value} [{top.fact_id}]."
        )
    else:
        answer = (
            f"These are the {len(items)} {summary_verb}-scoring {noun}s on "
            f"{_column_label(table, column)} ({table.get('metric_label')}). "
            f"“{top.name}” is first at {top.value} [{top.fact_id}]."
        )
    return {
        "status": "answered",
        "answer_text": answer,
        "blocks": [
            AssistantBlock(
                type="top_bottom_elements",
                title=title,
                data={"direction": summary_verb, "metric": table.get("metric_label"), "items": [i.model_dump() for i in items]},
            ).model_dump()
        ],
        "evidence": [_evidence_from_fact(f) for f in facts],
        "facts": facts,
        "follow_ups": ["What is the strongest segment?", "Show the best complete design"],
        "actions": [],
    }


# --------------------------------------------------------------------------- #
# Images / matching
# --------------------------------------------------------------------------- #

def _image_catalog(
    analysis: Dict[str, Any],
    study_obj: Study,
    metric: str,
) -> Dict[str, OptimizerElement]:
    try:
        categories = build_categories_from_analysis(
            analysis,
            metric=metric,
            segment_section=None,
            segment_key=None,
            study_type=str(getattr(study_obj, "study_type", None) or "grid"),
        )
        categories = enrich_categories_from_study_layers(categories, study_obj)
    except Exception:
        return {}
    catalog: Dict[str, OptimizerElement] = {}
    for cat in categories:
        for el in cat.elements or []:
            for key in (el.code, el.name, el.element_id, el.image_id):
                norm = _norm(key)
                if norm and norm not in catalog:
                    catalog[norm] = el
    return catalog


def _catalog_match(el: Dict[str, Any], catalog: Dict[str, OptimizerElement]) -> Optional[OptimizerElement]:
    for key in (el.get("code"), el.get("name")):
        hit = catalog.get(_norm(key))
        if hit:
            return hit
    return None


def _rank_item(
    rank: int,
    el: Dict[str, Any],
    catalog: Dict[str, OptimizerElement],
    value: float,
    fact_id: str,
) -> ElementRankItem:
    opt = _catalog_match(el, catalog)
    return ElementRankItem(
        rank=rank,
        element_id=(opt.element_id if opt else str(el.get("code") or el.get("name") or f"el-{rank}")),
        code=el.get("code") or (opt.code if opt else None),
        name=str(el.get("name") or ""),
        category=str(el.get("category") or (opt.category_name if opt else "")),
        value=_num(value),
        above_threshold=(el.get("above_threshold") or {}).get("Overall") if isinstance(el.get("above_threshold"), dict) else None,
        image_url=opt.image_url if opt else None,
        element_type=opt.element_type if opt else None,
        z_index=opt.z_index if opt else None,
        layer_id=opt.layer_id if opt else None,
        image_id=opt.image_id if opt else None,
        transform=opt.transform if opt else None,
        fact_id=fact_id,
    )


def _element_matches(el: Dict[str, Any], hints: Sequence[str]) -> bool:
    code = _norm(el.get("code"))
    name = _norm(el.get("name"))
    for hint in hints:
        h = _norm(hint)
        if not h:
            continue
        if h == code or h == name:
            return True
        if len(h) > 2 and (h in name or h in code):
            return True
    return False


def _resolve_column(columns: Sequence[Dict[str, Any]], hint: Any) -> Optional[str]:
    text = " ".join(str(hint or "").strip().split())
    if not text:
        return "Overall"
    folded = text.casefold()
    if folded in {"overall", "total", "all", "everyone", "whole sample"}:
        return "Overall"
    gender = extract_gender_from_text(text)
    if gender:
        key = f"Gender::{gender}"
        if any(c.get("key") == key for c in columns):
            return key
    age = resolve_age_segment_key(text) or extract_age_segment_from_text(text)
    if age:
        key = f"Age::{age}"
        if any(c.get("key") == key for c in columns):
            return key
    for col in columns:
        key = str(col.get("key") or "")
        label = str(col.get("label") or "")
        if folded == key.casefold() or folded == label.casefold():
            return key
    # Classification answers / question titles from the Combined sheet.
    exact_answer = None
    partial = None
    for col in columns:
        key = str(col.get("key") or "")
        label = str(col.get("label") or "")
        question, answer = _classification_parts(key)
        answer_fold = answer.casefold()
        question_fold = question.casefold()
        if answer_fold and (folded == answer_fold or folded in answer_fold or answer_fold in folded):
            if folded == answer_fold:
                exact_answer = key
                break
            partial = partial or key
        if question_fold and folded in question_fold:
            partial = partial or key
        if folded in key.casefold() or folded in label.casefold() or label.casefold() in folded:
            partial = partial or key
    return exact_answer or partial


def _unknown_column(table: Dict[str, Any], hint: Any) -> Dict[str, Any]:
    labels = [_column_label(table, c.get("key")) for c in (table.get("columns") or [])[:12]]
    return {
        "status": "answered",
        "answer_text": (
            f"I could not match “{hint}” to a Combined column. "
            f"Available: {', '.join(str(l) for l in labels if l)}."
        ),
        "blocks": [],
        "evidence": [],
        "facts": [],
        "clarification_options": [str(l) for l in labels if l][:8],
        "follow_ups": ["Rank Overall", "What is the strongest segment?"],
        "actions": [],
    }


def _not_found(wanted: Sequence[str], table: Dict[str, Any]) -> Dict[str, Any]:
    available = [f"{el.get('code')}: {el.get('name')}" for el in (table.get("elements") or [])[:16]]
    return {
        "status": "answered",
        "answer_text": (
            "No Combined row matched "
            + ", ".join(f"“{w}”" for w in wanted)
            + ((". Available: " + ", ".join(available)) if available else ".")
        ),
        "blocks": [],
        "evidence": [],
        "facts": [],
        "follow_ups": ["Show top elements overall"],
        "actions": [],
    }


def _label_for_combined_key(key: str) -> str:
    key = str(key or "").replace("\x00", "")
    if key == "Overall":
        return "Overall"
    if key.startswith("Gender::"):
        return key.split("::", 1)[1]
    if key.startswith("Age::"):
        return key.split("::", 1)[1]
    question, answer = _classification_parts(key)
    if answer:
        return f"{_short(question, 48)} — {_short(answer, 48)}" if question else answer
    return key.replace("::", " · ")


def _family_for_combined_key(key: str) -> str:
    if key == "Overall":
        return "Overall"
    if key.startswith("Gender::"):
        return "Gender"
    if key.startswith("Age::"):
        return "Age"
    if key.startswith("Classification::"):
        return "Classification"
    return "Other"


def _classification_parts(key: str) -> Tuple[str, str]:
    if not str(key).startswith("Classification::"):
        return "", ""
    rest = str(key)[len("Classification::") :]
    if "::" not in rest:
        return rest, ""
    question, answer = rest.split("::", 1)
    return question, answer


def _column_label(table: Dict[str, Any], key: Optional[str]) -> str:
    for col in table.get("columns") or []:
        if col.get("key") == key:
            return str(col.get("label") or key)
    return _label_for_combined_key(str(key or "Overall"))


def _evidence_from_fact(fact: Dict[str, Any]) -> Dict[str, Any]:
    return EvidenceFact(
        fact_id=str(fact.get("id") or fact.get("fact_id") or ""),
        label=str(fact.get("label") or ""),
        value=fact.get("value"),
        meta={k: v for k, v in fact.items() if k not in {"id", "fact_id", "label", "value"} and v is not None},
    ).model_dump()


def _element_type_for_study(study_obj: Study) -> str:
    kind = str(getattr(study_obj, "study_type", "") or "grid").lower()
    return "text" if kind == "text" else "image"


def _limit(args: Dict[str, Any], default: int = 5) -> int:
    try:
        value = int(args.get("limit") or default)
    except (TypeError, ValueError):
        value = default
    cap = max(1, int(getattr(settings, "ASSISTANT_MAX_RESULT_LIMIT", 20) or 20))
    return max(1, min(value, min(cap, _MAX_RESULT_ROWS)))


def _str_list(value: Any, cap: int = 12) -> List[str]:
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


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _short(value: Any, limit: int = 70) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
