"""
Fast task-count check. Arithmetic only: no matrix build, no database.

The generator already balances elements that can appear about as often as the
average element, so those studies stay at 1.5×. An element that is much rarer
than average (a large layer, or a layer constraint) is checked directly.
The search stops at 2.0×. If that still leaves missing images, generation
should not start.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

MAX_RESPONDENTS_CAP = 1500
MIN_MULTIPLIER = 1.5
MAX_MULTIPLIER = 2.0
MULTIPLIER_STEP = 0.05
# An element this far below the average appearance rate is not what the
# balancer can smooth over. 0.5 matches the lozenge failure and leaves
# evenly sized layers on the 1.5× path.
SCARCE_RATIO = 0.5
# Expected respondents who miss a scarce image. Above this, the rank check
# fails the whole job.
MAX_EXPECTED_FAILURES = 1.0


def generated_respondent_count(number_of_respondents: int) -> int:
    """Same count the golden-matrix generator builds."""
    requested = int(number_of_respondents or 0)
    if requested < 1:
        return 0
    if requested == 1:
        return 1
    return min(requested * 2, MAX_RESPONDENTS_CAP)


def _activation(kind: str, n_groups: int) -> Tuple[int, int, float]:
    if n_groups < 1:
        return 1, 1, 0.0
    min_a = min(2, n_groups)
    if kind == "layer":
        max_a = n_groups
    else:
        max_a = min(4, n_groups)
    if min_a > max_a:
        min_a, max_a = 1, max(1, max_a)
    avg_actives = (min_a + max_a) / 2.0
    p_on = avg_actives / n_groups
    return min_a, max_a, p_on


def _ref_key(ref: Any) -> Optional[Tuple[str, str]]:
    if not isinstance(ref, dict):
        return None
    layer_id = str(ref.get("layer_id") or ref.get("layerId") or "").strip()
    image_id = str(ref.get("image_id") or ref.get("imageId") or "").strip()
    if not layer_id or not image_id:
        return None
    return layer_id, image_id


def _groups_from_layers(layers: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, str], int]]:
    groups: List[Dict[str, Any]] = []
    ref_to_col: Dict[Tuple[str, str], int] = {}
    col = 0
    for layer in layers:
        images = [img for img in (layer.get("images") or []) if isinstance(img, dict)]
        if not images:
            continue
        layer_id = str(layer.get("layer_id") or layer.get("id") or layer.get("name") or "").strip()
        name = str(layer.get("name") or layer_id or "Layer")
        start = col
        for image_index, image in enumerate(images):
            image_id = str(
                image.get("image_id")
                or image.get("id")
                or image.get("name")
                or f"{image_index + 1}"
            ).strip()
            if layer_id and image_id:
                ref_to_col[(layer_id, image_id)] = col
            col += 1
        groups.append({"name": name, "start": start, "size": col - start})
    return groups, ref_to_col


def _groups_from_categories(
    categories: Sequence[Dict[str, Any]],
    elements: Sequence[Dict[str, Any]],
    phase_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    for cat in categories:
        if phase_type and str(cat.get("phase_type") or "") != phase_type:
            continue
        cat_id = str(cat.get("category_id") or cat.get("id") or "")
        matched = [
            el for el in elements
            if str(el.get("category_id") or "") == cat_id
        ]
        if not matched and not cat_id:
            continue
        if not matched:
            continue
        name = str(cat.get("name") or cat.get("category_name") or "Category")
        groups.append({"name": name, "start": 0, "size": len(matched)})
    # Column indexes are unused for grid constraints.
    cursor = 0
    for group in groups:
        group["start"] = cursor
        cursor += group["size"]
    return groups


def _blocker_keep(
    groups: Sequence[Dict[str, Any]],
    ref_to_col: Dict[Tuple[str, str], int],
    constraints: Sequence[Dict[str, Any]],
    p_on: float,
) -> List[float]:
    total = sum(int(g["size"]) for g in groups)
    keep = [1.0] * total
    if not constraints or total < 1 or p_on <= 0:
        return keep

    col_group_size = [0] * total
    for group in groups:
        size = int(group["size"])
        for offset in range(size):
            col_group_size[int(group["start"]) + offset] = size

    blocked_by: List[List[int]] = [[] for _ in range(total)]
    for constraint in constraints:
        if not isinstance(constraint, dict):
            continue
        anchors = constraint.get("anchors") or []
        blocked = constraint.get("blocked") or []
        anchor_cols = []
        for anchor in anchors:
            key = _ref_key(anchor)
            col = ref_to_col.get(key) if key else None
            if col is not None:
                anchor_cols.append(col)
        for blocked_ref in blocked:
            key = _ref_key(blocked_ref)
            col = ref_to_col.get(key) if key else None
            if col is None:
                continue
            for anchor_col in anchor_cols:
                if anchor_col != col:
                    blocked_by[col].append(anchor_col)

    for col, anchors in enumerate(blocked_by):
        if not anchors:
            continue
        chance_clear = 1.0
        seen = set()
        for anchor_col in anchors:
            if anchor_col in seen or anchor_col < 0 or anchor_col >= total:
                continue
            seen.add(anchor_col)
            size = col_group_size[anchor_col] or 1
            p_anchor = p_on / size
            chance_clear *= max(0.0, 1.0 - p_anchor)
        keep[col] = chance_clear
    return keep


def _element_rates(
    groups: Sequence[Dict[str, Any]],
    kind: str,
    keep: Sequence[float],
) -> Tuple[List[float], List[str], float]:
    n_groups = len(groups)
    _min_a, _max_a, p_on = _activation(kind, n_groups)
    total = sum(int(g["size"]) for g in groups)
    if total < 1 or p_on <= 0:
        return [], [], 0.0
    avg_actives = (_min_a + _max_a) / 2.0
    average_p = avg_actives / total
    rates: List[float] = []
    names: List[str] = []
    for group in groups:
        size = int(group["size"])
        base = p_on / size
        for offset in range(size):
            col = int(group["start"]) + offset
            factor = keep[col] if col < len(keep) else 1.0
            rates.append(base * factor)
            names.append(str(group["name"]))
    return rates, names, average_p


def _expected_failures(rates: Sequence[float], names: Sequence[str], average_p: float, tasks: int, respondents: int) -> Tuple[float, Optional[str]]:
    if respondents < 1 or tasks < 1 or not rates:
        return float("inf"), None
    log_survive = 0.0
    worst_name: Optional[str] = None
    worst_share = 0.0
    scarce_counts: Dict[str, int] = {}
    for rate, name in zip(rates, names):
        if average_p <= 0 or rate < SCARCE_RATIO * average_p:
            scarce_counts[name] = scarce_counts.get(name, 0) + 1
            miss = 1.0 if rate <= 0 else math.exp(-tasks * rate)
            # Union bound in log space so a long element list stays accurate.
            if miss >= 1:
                return float(respondents), name
            log_survive += math.log(1.0 - miss)
            if miss > worst_share:
                worst_share = miss
                worst_name = name
    if not scarce_counts:
        return 0.0, None
    respondent_fail = 1.0 - math.exp(log_survive)
    if worst_name is None:
        worst_name = max(scarce_counts, key=scarce_counts.get)
    return respondents * respondent_fail, worst_name


def _tasks_for(multiplier: float, total_elements: int) -> int:
    return max(total_elements, math.ceil(multiplier * total_elements - 1e-9))


def _search_phase(
    groups: Sequence[Dict[str, Any]],
    kind: str,
    keep: Sequence[float],
    respondents: int,
) -> Dict[str, Any]:
    rates, names, average_p = _element_rates(groups, kind, keep)
    total = len(rates)
    if total < 1:
        return {
            "can_generate": False,
            "multiplier": MIN_MULTIPLIER,
            "tasks_per_respondent": 0,
            "expected_failures": float("inf"),
            "scarce_layer": None,
            "total_elements": 0,
            "reason": "Add elements before generating tasks.",
        }

    best_fail = float("inf")
    best_name: Optional[str] = None
    hundredths = int(round(MIN_MULTIPLIER * 100))
    last = int(round(MAX_MULTIPLIER * 100))
    step = int(round(MULTIPLIER_STEP * 100))
    while hundredths <= last:
        multiplier = hundredths / 100.0
        tasks = _tasks_for(multiplier, total)
        expected, scarce_name = _expected_failures(rates, names, average_p, tasks, respondents)
        best_fail = expected
        best_name = scarce_name
        if expected < MAX_EXPECTED_FAILURES:
            return {
                "can_generate": True,
                "multiplier": multiplier,
                "tasks_per_respondent": tasks,
                "expected_failures": expected,
                "scarce_layer": scarce_name,
                "total_elements": total,
                "reason": "",
            }
        hundredths += step

    tasks = _tasks_for(MAX_MULTIPLIER, total)
    layer = best_name or "A layer"
    size = 0
    for group in groups:
        if group["name"] == layer:
            size = int(group["size"])
            break
    if size:
        reason = (
            f'This study cannot be generated within {MAX_MULTIPLIER:.1f}× tasks per respondent. '
            f'"{layer}" has {size} images, so some images will be missing for many respondents. '
            f'Remove images from that layer or loosen its design constraints.'
        )
    else:
        reason = (
            f'This study cannot be generated within {MAX_MULTIPLIER:.1f}× tasks per respondent. '
            f'Remove images from the largest layer or loosen its design constraints.'
        )
    return {
        "can_generate": False,
        "multiplier": MAX_MULTIPLIER,
        "tasks_per_respondent": tasks,
        "expected_failures": best_fail,
        "scarce_layer": layer,
        "total_elements": total,
        "reason": reason,
    }


def _phase_plan(payload: Dict[str, Any]) -> List[Tuple[str, str, List[Dict[str, Any]]]]:
    study_type = str(payload.get("study_type") or "grid").lower()
    if study_type == "layer":
        layers = [layer for layer in (payload.get("study_layers") or []) if isinstance(layer, dict)]
        return [("layer", "layer", layers)]

    categories = [cat for cat in (payload.get("categories") or []) if isinstance(cat, dict)]
    elements = [el for el in (payload.get("elements") or []) if isinstance(el, dict)]
    if study_type != "hybrid":
        kind = "grid"
        return [(study_type, kind, _groups_from_categories(categories, elements))]

    phase_order = [str(p) for p in (payload.get("phase_order") or []) if p and str(p) != "mix"]
    if not phase_order:
        seen = []
        for cat in categories:
            phase = str(cat.get("phase_type") or "grid")
            if phase not in seen:
                seen.append(phase)
        phase_order = seen or ["grid"]
    return [
        (phase, "grid", _groups_from_categories(categories, elements, phase))
        for phase in phase_order
    ]


def precheck_task_generation(payload: Dict[str, Any]) -> Dict[str, Any]:
    audience = payload.get("audience_segmentation") or {}
    requested = 0
    if isinstance(audience, dict):
        requested = int(audience.get("number_of_respondents") or 0)
    respondents = generated_respondent_count(requested)
    if respondents < 1:
        return {
            "can_generate": False,
            "multiplier": MIN_MULTIPLIER,
            "tasks_per_respondent": 0,
            "generated_respondents": 0,
            "total_elements": 0,
            "reason": "Set the number of respondents before generating tasks.",
            "scarce_layer": None,
            "expected_failures": 0.0,
        }

    study_type = str(payload.get("study_type") or "grid").lower()
    constraints = payload.get("design_constraints") or []
    if not isinstance(constraints, list):
        constraints = []

    phases = _phase_plan(payload)
    usable = [(name, kind, groups) for name, kind, groups in phases if groups]
    if not usable:
        return {
            "can_generate": False,
            "multiplier": MIN_MULTIPLIER,
            "tasks_per_respondent": 0,
            "generated_respondents": respondents,
            "total_elements": 0,
            "reason": "Add elements before generating tasks.",
            "scarce_layer": None,
            "expected_failures": 0.0,
        }

    chosen: List[Dict[str, Any]] = []
    total_elements = 0
    for phase_name, kind, raw_groups in usable:
        if study_type == "layer":
            groups, ref_to_col = _groups_from_layers(raw_groups)
            _min_a, _max_a, p_on = _activation("layer", len(groups))
            keep = _blocker_keep(groups, ref_to_col, constraints, p_on)
            result = _search_phase(groups, "layer", keep, respondents)
        else:
            groups = [dict(group) for group in raw_groups]
            if study_type == "hybrid":
                for group in groups:
                    group["name"] = f'{phase_name}: {group["name"]}'
            keep = [1.0] * sum(int(g["size"]) for g in groups)
            result = _search_phase(groups, "grid", keep, respondents)
        total_elements += int(result["total_elements"])
        chosen.append(result)
        if not result["can_generate"]:
            failed = dict(result)
            failed["generated_respondents"] = respondents
            failed["total_elements"] = total_elements
            return failed

    multiplier = max(float(item["multiplier"]) for item in chosen)
    tasks = max(int(item["tasks_per_respondent"]) for item in chosen)
    expected = max(float(item["expected_failures"]) for item in chosen)
    scarce = next((item["scarce_layer"] for item in chosen if item.get("scarce_layer")), None)
    if abs(multiplier - MIN_MULTIPLIER) < 1e-9:
        reason = f"Tasks can be generated at {MIN_MULTIPLIER:.1f}× ({tasks} tasks per respondent)."
    else:
        reason = (
            f"Tasks can be generated at {multiplier:.2f}× ({tasks} tasks per respondent)."
        )
    return {
        "can_generate": True,
        "multiplier": multiplier,
        "tasks_per_respondent": tasks,
        "generated_respondents": respondents,
        "total_elements": total_elements,
        "reason": reason,
        "scarce_layer": scarce,
        "expected_failures": expected,
    }
