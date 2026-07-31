"""
Canonical deterministic top-K / bottom-K design and element optimizer.

All study types (grid, text, hybrid, layer) use the same scoring rules:
- Score = sum of selected element coefficients from analysis JSON
- Layer default: exactly one element from every layer (complete designs)
- Design constraints are always applied for layer studies
- Deterministic tie-breaking for reproducible answers
"""

from __future__ import annotations

import heapq
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

ALGORITHM_VERSION = "1.1.0"
MAX_NON_LAYER_SELECTIONS = 4


@dataclass(frozen=True)
class OptimizerElement:
    element_id: str
    category_key: str
    category_name: str
    name: str
    value: float
    code: Optional[str] = None
    image_url: Optional[str] = None
    element_type: Optional[str] = None
    z_index: int = 0
    category_order: int = 0
    layer_id: Optional[str] = None
    image_id: Optional[str] = None
    transform: Optional[Dict[str, Any]] = None
    above_threshold: Optional[bool] = None


@dataclass
class OptimizerCategory:
    key: str
    name: str
    z_index: int = 0
    order: int = 0
    elements: List[OptimizerElement] = field(default_factory=list)


@dataclass
class RankedDesign:
    rank: int
    score: float
    selected_by_category: Dict[str, str]
    elements: List[OptimizerElement]
    constraints_applied: bool
    complete_layers: bool


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def metric_prefix(metric: str) -> str:
    key = (metric or "T").upper()
    if key in {"T", "TOP", "TOP DOWN", "TOP_DOWN"}:
        return "T"
    if key in {"B", "BOTTOM", "BOTTOM UP", "BOTTOM_UP"}:
        return "B"
    if key in {"R", "RESPONSE", "RESPONSE TIME", "RESPONSE_TIME"}:
        return "R"
    return "T"


def section_key_for(metric: str, segment_section: Optional[str] = None) -> str:
    prefix = metric_prefix(metric)
    section = (segment_section or "Overall").strip()
    aliases = {
        "overall": "Overall",
        "age": "Age",
        "gender": "Gender",
        "mindsets": "Mindsets",
        "mindset": "Mindsets",
        "prelim": "Classification Questions",
        "classification": "Classification Questions",
        "classification questions": "Classification Questions",
    }
    normalized = aliases.get(section.lower(), section)
    if normalized.startswith("("):
        return normalized
    return f"({prefix}) {normalized}"


def constraint_ref_key(ref: Dict[str, Any]) -> Optional[str]:
    layer_id = _normalize_text(ref.get("layer_id") or ref.get("layerId"))
    image_id = _normalize_text(ref.get("image_id") or ref.get("imageId"))
    if layer_id and image_id:
        return f"{layer_id}::{image_id}"
    return None


def element_constraint_key(element: OptimizerElement) -> Optional[str]:
    if element.layer_id and element.image_id:
        return f"{element.layer_id}::{element.image_id}"
    return None


def _layer_alias_keys(layer: Any) -> List[str]:
    keys: List[str] = []
    for value in (
        getattr(layer, "layer_id", None),
        getattr(layer, "id", None),
        getattr(layer, "name", None),
        getattr(layer, "title", None),
        layer.get("layer_id") if isinstance(layer, dict) else None,
        layer.get("id") if isinstance(layer, dict) else None,
        layer.get("name") if isinstance(layer, dict) else None,
    ):
        text = _normalize_text(value)
        if text:
            keys.append(text)
            keys.append(text.casefold())
    return list(dict.fromkeys(keys))


def _image_alias_keys(image: Any) -> List[str]:
    keys: List[str] = []
    for value in (
        getattr(image, "image_id", None),
        getattr(image, "id", None),
        getattr(image, "name", None),
        getattr(image, "alt_text", None),
        image.get("image_id") if isinstance(image, dict) else None,
        image.get("id") if isinstance(image, dict) else None,
        image.get("name") if isinstance(image, dict) else None,
    ):
        text = _normalize_text(value)
        if text:
            keys.append(text)
            keys.append(text.casefold())
    return list(dict.fromkeys(keys))


def canonicalize_design_constraints(
    design_constraints: Optional[Sequence[Dict[str, Any]]],
    layers: Optional[Sequence[Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Normalize constraint refs so they match configurator / task-generator IDs.

    Constraints may store either:
      - study_layers.layer_id + layer_images.image_id, or
      - ORM/primary-key UUIDs, or
      - camelCase layerId/imageId from the create-study UI.
    """
    if not design_constraints:
        return []

    alias_to_canonical: Dict[str, str] = {}
    for layer in layers or []:
        layer_id = _normalize_text(
            getattr(layer, "layer_id", None)
            if not isinstance(layer, dict)
            else layer.get("layer_id")
        ) or _normalize_text(
            getattr(layer, "id", None) if not isinstance(layer, dict) else layer.get("id")
        )
        images = (
            list(getattr(layer, "images", None) or [])
            if not isinstance(layer, dict)
            else list(layer.get("images") or [])
        )
        for image in images:
            image_id = _normalize_text(
                getattr(image, "image_id", None)
                if not isinstance(image, dict)
                else image.get("image_id")
            ) or _normalize_text(
                getattr(image, "id", None) if not isinstance(image, dict) else image.get("id")
            )
            if not layer_id or not image_id:
                continue
            canonical = f"{layer_id}::{image_id}"
            for layer_alias in _layer_alias_keys(layer):
                for image_alias in _image_alias_keys(image):
                    alias_to_canonical[f"{layer_alias}::{image_alias}"] = canonical
                    alias_to_canonical[f"{layer_alias.casefold()}::{image_alias.casefold()}"] = canonical

    normalized: List[Dict[str, Any]] = []
    for constraint in design_constraints:
        if not isinstance(constraint, dict):
            continue
        anchors_out: List[Dict[str, str]] = []
        blocked_out: List[Dict[str, str]] = []
        for side, target in (
            (constraint.get("anchors") or [], anchors_out),
            (constraint.get("blocked") or [], blocked_out),
        ):
            for ref in side:
                if not isinstance(ref, dict):
                    continue
                raw_key = constraint_ref_key(ref)
                if not raw_key:
                    continue
                canonical = (
                    alias_to_canonical.get(raw_key)
                    or alias_to_canonical.get(raw_key.casefold())
                    or raw_key
                )
                layer_id, image_id = canonical.split("::", 1)
                target.append({"layer_id": layer_id, "image_id": image_id})
        if anchors_out and blocked_out:
            item = dict(constraint)
            item["anchors"] = anchors_out
            item["blocked"] = blocked_out
            normalized.append(item)
    return normalized


def build_conflict_pair_set(design_constraints: Optional[Sequence[Dict[str, Any]]]) -> Set[str]:
    pairs: Set[str] = set()
    for constraint in design_constraints or []:
        anchors = constraint.get("anchors") or []
        blocked = constraint.get("blocked") or []
        for anchor in anchors:
            anchor_key = constraint_ref_key(anchor if isinstance(anchor, dict) else {})
            if not anchor_key:
                continue
            for blocked_ref in blocked:
                blocked_key = constraint_ref_key(blocked_ref if isinstance(blocked_ref, dict) else {})
                if not blocked_key or blocked_key == anchor_key:
                    continue
                pairs.add(f"{anchor_key}|{blocked_key}")
                pairs.add(f"{blocked_key}|{anchor_key}")
    return pairs


def conflicts_with_selected(
    element: OptimizerElement,
    selected: Sequence[OptimizerElement],
    conflict_pairs: Set[str],
) -> bool:
    element_key = element_constraint_key(element)
    if not element_key:
        return False
    for selected_el in selected:
        selected_key = element_constraint_key(selected_el)
        if selected_key and f"{element_key}|{selected_key}" in conflict_pairs:
            return True
    return False


def _extract_element_value(element: Dict[str, Any], segment_key: Optional[str]) -> Tuple[float, Optional[bool]]:
    if segment_key:
        values = element.get("values") or {}
        above = element.get("above_threshold") or {}
        if isinstance(values, dict) and segment_key in values:
            value_obj = values.get(segment_key)
            if isinstance(value_obj, dict):
                return _to_float(value_obj.get("value")), bool(value_obj.get("above_threshold")) if "above_threshold" in value_obj else (
                    bool(above.get(segment_key)) if isinstance(above, dict) else None
                )
            return _to_float(value_obj), bool(above.get(segment_key)) if isinstance(above, dict) else None
    return _to_float(element.get("value")), (
        bool(element.get("above_threshold")) if isinstance(element.get("above_threshold"), bool) else None
    )


def _info_block_element_lookup(analysis: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    info = analysis.get("Information Block") or {}
    lookup: Dict[str, Dict[str, Any]] = {}
    for category in info.get("Categories") or []:
        cat_name = _normalize_text(category.get("name") or category.get("Name") or "")
        for element in category.get("elements") or category.get("Elements") or []:
            name = _normalize_text(element.get("name") or element.get("Name") or "")
            key = f"{cat_name}::{name}".lower()
            lookup[key] = element if isinstance(element, dict) else {}
    return lookup


def build_categories_from_analysis(
    analysis: Dict[str, Any],
    metric: str = "T",
    segment_section: Optional[str] = None,
    segment_key: Optional[str] = None,
    study_type: str = "grid",
) -> List[OptimizerCategory]:
    section = analysis.get(section_key_for(metric, segment_section)) or {}
    categories_raw = section.get("categories") or []
    info_lookup = _info_block_element_lookup(analysis)
    result: List[OptimizerCategory] = []

    for cat_index, category in enumerate(categories_raw):
        cat_name = _normalize_text(category.get("name") or f"Category {cat_index + 1}")
        cat_key = f"{cat_index}:{cat_name}"
        z_index = int(_to_float(category.get("z_index") or category.get("zIndex") or cat_index, cat_index))
        elements: List[OptimizerElement] = []
        for el_index, element in enumerate(category.get("elements") or []):
            name = _normalize_text(element.get("name") or f"Element {el_index + 1}")
            value, above = _extract_element_value(element, segment_key)
            info = info_lookup.get(f"{cat_name}::{name}".lower(), {})
            transform = element.get("transform") or info.get("transform")
            layer_id = _normalize_text(element.get("layer_id") or info.get("layer_id") or "") or None
            image_id = _normalize_text(element.get("image_id") or info.get("image_id") or "") or None
            image_url = (
                element.get("content")
                or element.get("url")
                or element.get("image_url")
                or info.get("content")
                or info.get("url")
                or info.get("image_url")
            )
            element_type = element.get("element_type") or info.get("element_type")
            if not element_type and isinstance(image_url, str) and image_url.startswith("http"):
                element_type = "image"
            if study_type == "text":
                element_type = element_type or "text"
            element_z = int(_to_float(element.get("z_index") or info.get("z_index") or z_index, z_index))
            element_id = f"{cat_key}::{name}"
            elements.append(
                OptimizerElement(
                    element_id=element_id,
                    category_key=cat_key,
                    category_name=cat_name,
                    name=name,
                    value=value,
                    code=_normalize_text(element.get("code")) or None,
                    image_url=image_url if isinstance(image_url, str) else None,
                    element_type=element_type,
                    z_index=element_z,
                    category_order=cat_index,
                    layer_id=layer_id,
                    image_id=image_id,
                    transform=transform if isinstance(transform, dict) else None,
                    above_threshold=above,
                )
            )
        if elements:
            result.append(
                OptimizerCategory(
                    key=cat_key,
                    name=cat_name,
                    z_index=z_index,
                    order=cat_index,
                    elements=elements,
                )
            )
    return result


def _element_sort_key(element: OptimizerElement, descending: bool) -> Tuple:
    # Stable deterministic ordering
    return (
        -element.value if descending else element.value,
        element.z_index if descending is False else element.z_index,
        element.category_order,
        element.category_name.lower(),
        element.name.lower(),
        (element.code or "").lower(),
        element.element_id.lower(),
    )


def rank_elements(
    categories: Sequence[OptimizerCategory],
    *,
    direction: str = "highest",
    limit: int = 10,
) -> List[OptimizerElement]:
    descending = direction != "lowest"
    flat: List[OptimizerElement] = []
    for category in categories:
        flat.extend(category.elements)
    flat.sort(key=lambda el: _element_sort_key(el, descending))
    if not flat:
        return []
    k = max(1, min(int(limit), len(flat)))
    boundary = flat[k - 1].value
    selected = [el for el in flat if (el.value >= boundary if descending else el.value <= boundary)]
    # Cap expansion at 2x requested limit to avoid huge tie groups
    return selected[: max(k, min(len(selected), k * 2))]


def _design_signature(selected_by_category: Dict[str, str]) -> str:
    return "|".join(f"{key}={selected_by_category[key]}" for key in sorted(selected_by_category.keys()))


def _selection_to_elements(
    categories: Sequence[OptimizerCategory],
    selected_by_category: Dict[str, str],
) -> List[OptimizerElement]:
    by_id: Dict[str, OptimizerElement] = {}
    for category in categories:
        for element in category.elements:
            by_id[element.element_id] = element
    elements = [by_id[eid] for eid in selected_by_category.values() if eid in by_id]
    elements.sort(key=lambda el: (el.z_index, el.category_order, el.category_name.lower(), el.name.lower()))
    return elements


def _push_design(
    heap: List[Tuple],
    seen: Set[str],
    *,
    score: float,
    selected_by_category: Dict[str, str],
    maximize: bool,
    limit: int,
) -> None:
    if not selected_by_category:
        return
    signature = _design_signature(selected_by_category)
    if signature in seen:
        return
    seen.add(signature)
    # For maximize keep a min-heap of size K (worst of best). For minimize keep max-heap via negation.
    tie_key = signature
    if maximize:
        item = (score, tie_key, dict(selected_by_category))
        if len(heap) < limit:
            heapq.heappush(heap, item)
        elif score > heap[0][0] or (score == heap[0][0] and tie_key < heap[0][1]):
            heapq.heapreplace(heap, item)
    else:
        item = (-score, tie_key, dict(selected_by_category))
        if len(heap) < limit:
            heapq.heappush(heap, item)
        elif score < -heap[0][0] or (score == -heap[0][0] and tie_key < heap[0][1]):
            heapq.heapreplace(heap, item)


def rank_designs(
    categories: Sequence[OptimizerCategory],
    *,
    study_type: str,
    direction: str = "highest",
    limit: int = 10,
    design_constraints: Optional[Sequence[Dict[str, Any]]] = None,
    require_all_layers: bool = True,
    timeout_ms: int = 200,
    forced_by_category: Optional[Dict[str, str]] = None,
    require_any_element_ids: Optional[Sequence[str]] = None,
) -> Tuple[List[RankedDesign], Dict[str, Any]]:
    """
    Rank valid designs.

    Layer:
      - exactly one element per layer when require_all_layers=True (default)
      - always apply design constraints
    Grid/text/hybrid:
      - at most one element per category
      - at most MAX_NON_LAYER_SELECTIONS categories

    Optional user ingredients:
      - forced_by_category: category_key -> element_id that must be used
      - require_any_element_ids: final design must contain at least one of these
    """
    start = time.perf_counter()
    maximize = direction != "lowest"
    limit = max(1, min(int(limit), 20))
    is_layer = (study_type or "").lower() == "layer"
    conflict_pairs = build_conflict_pair_set(design_constraints) if is_layer else set()
    forced = {str(k): str(v) for k, v in (forced_by_category or {}).items() if k and v}
    require_any = {str(eid) for eid in (require_any_element_ids or []) if eid}
    meta: Dict[str, Any] = {
        "algorithm_version": ALGORITHM_VERSION,
        "constraints_applied": bool(conflict_pairs) or is_layer,
        "require_all_layers": bool(require_all_layers and is_layer),
        "forced_count": len(forced),
        "require_any_count": len(require_any),
        "timed_out": False,
        "explored": 0,
    }

    usable = [c for c in categories if c.elements]
    if not usable:
        return [], meta

    # Validate forced selections exist; drop invalid keys so search can still run.
    valid_forced: Dict[str, str] = {}
    for category in usable:
        forced_id = forced.get(category.key)
        if not forced_id:
            continue
        if any(el.element_id == forced_id for el in category.elements):
            valid_forced[category.key] = forced_id
    forced = valid_forced
    meta["forced_count"] = len(forced)

    heap: List[Tuple] = []
    seen: Set[str] = set()

    def _passes_require_any(selected_map: Dict[str, str]) -> bool:
        if not require_any:
            return True
        return any(eid in require_any for eid in selected_map.values())

    if is_layer:
        layer_categories = sorted(
            usable,
            key=lambda c: (
                -_conflict_degree(c, conflict_pairs),
                0 if c.key in forced else 1,
                len(c.elements),
                c.z_index,
                c.name.lower(),
            ),
        )
        # Explore best-first for maximize and worst-first for minimize so the
        # branch-and-bound finds strong candidates early, prunes aggressively,
        # and reaches the true optimum well before the timeout. Exploring in the
        # wrong order (e.g. highest-first while minimizing) can cause a timeout
        # to return a wildly non-optimal "worst" design.
        ordered_elements = []
        for cat in layer_categories:
            elements = sorted(cat.elements, key=lambda el: _element_sort_key(el, maximize))
            if cat.key in forced:
                elements = [el for el in elements if el.element_id == forced[cat.key]]
            ordered_elements.append(elements)

        # Suffix bound for pruning (optimistic remaining score). Computed from
        # explicit per-layer min/max so it stays correct regardless of the
        # exploration order chosen above.
        suffix_best = [0.0] * (len(layer_categories) + 1)
        for idx in range(len(layer_categories) - 1, -1, -1):
            values = [el.value for el in ordered_elements[idx]]
            layer_max = max(values) if values else 0.0
            layer_min = min(values) if values else 0.0
            # For maximize, remaining can add up to max(0, best); for minimize, down to min(0, worst)
            if maximize:
                suffix_best[idx] = suffix_best[idx + 1] + max(0.0, layer_max)
            else:
                suffix_best[idx] = suffix_best[idx + 1] + min(0.0, layer_min)

        def search(index: int, selected: List[OptimizerElement], selected_map: Dict[str, str], score: float) -> None:
            meta["explored"] += 1
            elapsed_ms = (time.perf_counter() - start) * 1000
            if elapsed_ms > timeout_ms:
                meta["timed_out"] = True
                return
            if maximize:
                optimistic = score + suffix_best[index]
                if len(heap) >= limit and optimistic < heap[0][0]:
                    return
            else:
                optimistic = score + suffix_best[index]
                if len(heap) >= limit and optimistic > -heap[0][0]:
                    return

            if index == len(layer_categories):
                if require_all_layers and len(selected_map) != len(layer_categories):
                    return
                if selected_map and _passes_require_any(selected_map):
                    _push_design(
                        heap,
                        seen,
                        score=score,
                        selected_by_category=selected_map,
                        maximize=maximize,
                        limit=limit,
                    )
                return

            category = layer_categories[index]
            # Optional skip only when not requiring all layers and category is not forced
            if not require_all_layers and category.key not in forced:
                search(index + 1, selected, selected_map, score)

            for element in ordered_elements[index]:
                if conflicts_with_selected(element, selected, conflict_pairs):
                    continue
                selected_map[category.key] = element.element_id
                selected.append(element)
                search(index + 1, selected, selected_map, score + element.value)
                selected.pop()
                del selected_map[category.key]
                if meta["timed_out"]:
                    return

        search(0, [], {}, 0.0)
    else:
        # Non-layer configurator parity: use exactly one element from each of
        # up to four categories. A "design" therefore stays comparable to the
        # grid/text/hybrid configurator instead of winning by selecting only
        # one positive element and silently omitting the rest.
        forced_cats = [c for c in usable if c.key in forced]
        free_cats = [c for c in usable if c.key not in forced]
        ranked_free = sorted(
            free_cats,
            key=lambda c: (
                (
                    -max(el.value for el in c.elements)
                    if maximize
                    else min(el.value for el in c.elements)
                ),
                c.order,
                c.name.lower(),
            ),
        )
        # Forced categories always participate; fill remaining slots from free cats.
        target_count = min(MAX_NON_LAYER_SELECTIONS, len(usable))
        if forced_cats:
            target_count = max(len(forced_cats), min(target_count, MAX_NON_LAYER_SELECTIONS))
        candidate_free = ranked_free[: max(MAX_NON_LAYER_SELECTIONS * 3, MAX_NON_LAYER_SELECTIONS)]

        def search_non_layer(
            start_idx: int,
            selected: List[OptimizerElement],
            selected_map: Dict[str, str],
            score: float,
            forced_done: bool,
        ) -> None:
            meta["explored"] += 1
            if (time.perf_counter() - start) * 1000 > timeout_ms:
                meta["timed_out"] = True
                return
            if not forced_done:
                # Seed search with all forced elements first.
                next_map = dict(selected_map)
                next_selected = list(selected)
                next_score = score
                for category in forced_cats:
                    forced_id = forced[category.key]
                    element = next((el for el in category.elements if el.element_id == forced_id), None)
                    if not element:
                        return
                    next_map[category.key] = element.element_id
                    next_selected.append(element)
                    next_score += element.value
                if len(next_map) >= target_count:
                    if _passes_require_any(next_map):
                        _push_design(
                            heap,
                            seen,
                            score=next_score,
                            selected_by_category=next_map,
                            maximize=maximize,
                            limit=limit,
                        )
                    return
                search_non_layer(0, next_selected, next_map, next_score, True)
                return

            if len(selected_map) == target_count:
                if _passes_require_any(selected_map):
                    _push_design(
                        heap,
                        seen,
                        score=score,
                        selected_by_category=selected_map,
                        maximize=maximize,
                        limit=limit,
                    )
                return
            for idx in range(start_idx, len(candidate_free)):
                category = candidate_free[idx]
                elements = sorted(
                    category.elements,
                    key=lambda el: _element_sort_key(el, maximize),
                )
                for element in elements:
                    selected_map[category.key] = element.element_id
                    selected.append(element)
                    search_non_layer(idx + 1, selected, selected_map, score + element.value, True)
                    selected.pop()
                    del selected_map[category.key]
                    if meta["timed_out"]:
                        return

        search_non_layer(0, [], {}, 0.0, False)

    # Materialize ranked designs.
    # maximize heap item: (score, tie, map) — sort by score desc, tie asc
    # minimize heap item: (-score, tie, map) — sort by -score desc => score asc, tie asc
    ordered = sorted(heap, key=lambda item: (-item[0], item[1]))

    designs: List[RankedDesign] = []
    for rank, item in enumerate(ordered[:limit], start=1):
        score = item[0] if maximize else -item[0]
        selected_map = item[2]
        elements = _selection_to_elements(usable, selected_map)
        designs.append(
            RankedDesign(
                rank=rank,
                score=float(score),
                selected_by_category=selected_map,
                elements=elements,
                constraints_applied=bool(conflict_pairs) or is_layer,
                complete_layers=bool(is_layer and require_all_layers and len(selected_map) == len(usable)),
            )
        )

    meta["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 2)
    meta["result_count"] = len(designs)
    return designs, meta


def _conflict_degree(category: OptimizerCategory, conflict_pairs: Set[str]) -> int:
    count = 0
    for element in category.elements:
        key = element_constraint_key(element)
        if not key:
            continue
        prefix = f"{key}|"
        for pair in conflict_pairs:
            if pair.startswith(prefix):
                count += 1
    return count


def verify_design(
    design: RankedDesign,
    conflict_pairs: Set[str],
    *,
    require_all_layers: bool,
    layer_count: int,
) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    recomputed = sum(el.value for el in design.elements)
    if abs(recomputed - design.score) > 1e-6:
        errors.append("score_mismatch")
    if len(design.elements) != len(design.selected_by_category):
        errors.append("selection_count_mismatch")
    if require_all_layers and len(design.selected_by_category) != layer_count:
        errors.append("incomplete_layers")
    # uniqueness of categories
    cats = [el.category_key for el in design.elements]
    if len(cats) != len(set(cats)):
        errors.append("duplicate_category")
    # constraints
    for i, left in enumerate(design.elements):
        for right in design.elements[i + 1 :]:
            left_key = element_constraint_key(left)
            right_key = element_constraint_key(right)
            if left_key and right_key and f"{left_key}|{right_key}" in conflict_pairs:
                errors.append("constraint_violation")
                break
    return len(errors) == 0, errors
