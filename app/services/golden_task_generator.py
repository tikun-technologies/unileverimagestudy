"""
Golden Matrix–based task generation matching legacy v2 payload shape (tasks + metadata).

Uses 2× respondent count (cap 1500) like task_generation_core.generate_*_tasks_v2.
"""
from __future__ import annotations

import logging
import math
from itertools import combinations
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

from app.services.golden_matrix.config import DesignConfig
from app.services.golden_matrix.exceptions import InfeasibleConfigError, RDEError
from app.services.golden_matrix.generator import generate_golden_matrix

logger = logging.getLogger(__name__)

MAX_RESPONDENTS_CAP = 1500


def _requested_to_generated_n(number_of_respondents: int) -> int:
    if number_of_respondents < 1:
        return 0
    return min(number_of_respondents * 2, MAX_RESPONDENTS_CAP)


def _category_info_from_categories(categories_data: List[Dict]) -> Dict[str, List[str]]:
    return {
        c["category_name"]: [f"{c['category_name']}_{j + 1}" for j in range(len(c.get("elements") or []))]
        for c in categories_data
    }


def _category_info_from_layers(layers_data: List[Dict]) -> Dict[str, List[str]]:
    return {
        layer["name"]: [f"{layer['name']}_{j + 1}" for j in range(len(layer.get("images") or []))]
        for layer in layers_data
    }


def _filter_nonempty_categories(categories_data: List[Dict]) -> List[Dict]:
    return [c for c in categories_data if c.get("elements")]


def _filter_nonempty_layers(layers_data: List[Dict]) -> List[Dict]:
    return [L for L in layers_data if L.get("images")]


def _column_mapping_categories(categories_data: List[Dict]) -> List[Tuple[str, int, str]]:
    """(category_name, element_index_0based, synthetic_key) per column of X, left-to-right."""
    mapping: List[Tuple[str, int, str]] = []
    for cat in categories_data:
        name = cat["category_name"]
        for j in range(len(cat.get("elements") or [])):
            mapping.append((name, j, f"{name}_{j + 1}"))
    return mapping


def _column_mapping_layers(layers_data: List[Dict]) -> List[Tuple[str, int, str]]:
    mapping: List[Tuple[str, int, str]] = []
    for layer in layers_data:
        name = layer["name"]
        for j in range(len(layer.get("images") or [])):
            mapping.append((name, j, f"{name}_{j + 1}"))
    return mapping


def _build_design_config_grid(
    n_gen: int,
    categories_data: List[Dict],
    tasks_per_respondent: int,
) -> DesignConfig:
    counts = [len(c["elements"]) for c in categories_data]
    c = len(counts)
    min_a = min(2, c) if c >= 1 else 1
    max_a = min(4, c) if c >= 1 else 1
    if min_a > max_a:
        min_a, max_a = 1, max(1, max_a)
    return DesignConfig(
        n_respondents=n_gen,
        tasks_per_respondent=tasks_per_respondent,
        n_categories=c,
        elements_per_category=counts,
        min_actives_per_row=min_a,
        max_actives_per_row=max_a,
    )


def _build_design_config_layer(
    n_gen: int,
    layers_data: List[Dict],
    tasks_per_respondent: int,
    incompatible_pairs: Optional[List[Tuple[int, int]]] = None,
) -> DesignConfig:
    counts = [len(L["images"]) for L in layers_data]
    c = len(counts)
    min_a = min(2, c) if c >= 1 else 1
    max_a = c if c >= 1 else 1
    if min_a > max_a:
        min_a, max_a = 1, max(1, max_a)
    return DesignConfig(
        n_respondents=n_gen,
        tasks_per_respondent=tasks_per_respondent,
        n_categories=c,
        elements_per_category=counts,
        min_actives_per_row=min_a,
        max_actives_per_row=max_a,
        incompatible_pairs=incompatible_pairs or [],
    )


def _constraint_ref_key(ref: Any) -> Optional[Tuple[str, str]]:
    if not isinstance(ref, dict):
        return None
    layer_id = str(ref.get("layer_id") or ref.get("layerId") or "").strip()
    image_id = str(ref.get("image_id") or ref.get("imageId") or "").strip()
    if not layer_id or not image_id:
        return None
    return layer_id, image_id


def _layer_constraint_pairs(
    layers_data: List[Dict],
    design_constraints: Optional[List[Dict[str, Any]]],
) -> Tuple[List[Tuple[int, int]], int]:
    if not design_constraints:
        return [], 0

    ref_to_col: Dict[Tuple[str, str], int] = {}
    col_idx = 0
    for layer in layers_data:
        layer_id = str(layer.get("layer_id") or layer.get("id") or layer.get("name") or "").strip()
        for image_index, image in enumerate(layer.get("images") or []):
            image_id = str(
                image.get("image_id")
                or image.get("id")
                or image.get("name")
                or f"{image_index + 1}"
            ).strip()
            if layer_id and image_id:
                ref_to_col[(layer_id, image_id)] = col_idx
            col_idx += 1

    pair_keys: Set[Tuple[int, int]] = set()
    skipped = 0
    for constraint in design_constraints:
        if not isinstance(constraint, dict):
            skipped += 1
            continue
        anchors = constraint.get("anchors") or []
        blocked = constraint.get("blocked") or []
        for anchor in anchors:
            anchor_key = _constraint_ref_key(anchor)
            anchor_col = ref_to_col.get(anchor_key) if anchor_key else None
            if anchor_col is None:
                skipped += 1
                continue
            for blocked_ref in blocked:
                blocked_key = _constraint_ref_key(blocked_ref)
                blocked_col = ref_to_col.get(blocked_key) if blocked_key else None
                if blocked_col is None or blocked_col == anchor_col:
                    skipped += 1
                    continue
                pair_keys.add(tuple(sorted((anchor_col, blocked_col))))

    return sorted(pair_keys), skipped


def validate_layer_design_constraints_fast(
    layers_data: List[Dict],
    design_constraints: Optional[List[Dict[str, Any]]] = None,
    tasks_per_respondent: int = 0,
) -> Dict[str, Any]:
    layers = _filter_nonempty_layers(layers_data)
    if not layers:
        return {
            "feasible": False,
            "reason": "Layer study requires at least one layer with images.",
            "valid_row_variety": 0,
            "required_row_variety": 0,
            "tasks_per_respondent": 0,
            "constraints_checked": len(design_constraints or []),
            "incompatible_pairs_count": 0,
            "skipped_constraint_refs": 0,
            "row_universe_rank": 0,
            "required_rank": 0,
            "suggestions": ["Add images to each layer before generating tasks."],
        }

    counts = [len(layer.get("images") or []) for layer in layers]
    total_elements = sum(counts)
    if total_elements < 1:
        return {
            "feasible": False,
            "reason": "Layer study requires at least one image.",
            "valid_row_variety": 0,
            "required_row_variety": 0,
            "tasks_per_respondent": 0,
            "constraints_checked": len(design_constraints or []),
            "incompatible_pairs_count": 0,
            "skipped_constraint_refs": 0,
            "row_universe_rank": 0,
            "required_rank": 0,
            "suggestions": ["Add more layer images before generating tasks."],
        }

    tpr = max(0, int(tasks_per_respondent or 0))
    if tpr == 0:
        tpr = math.ceil(1.5 * total_elements)
    required_variety = max(1, 2 * tpr)

    def make_response(
        feasible: bool,
        reason: str,
        valid_row_variety: int,
        row_universe_rank: int,
        suggestions: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return {
            "feasible": feasible,
            "reason": reason,
            "valid_row_variety": valid_row_variety,
            "required_row_variety": required_variety,
            "tasks_per_respondent": tpr,
            "constraints_checked": len(design_constraints or []),
            "incompatible_pairs_count": len(incompatible_pairs),
            "skipped_constraint_refs": skipped_constraints,
            "row_universe_rank": row_universe_rank,
            "required_rank": total_elements,
            "suggestions": suggestions or [],
        }

    incompatible_pairs, skipped_constraints = _layer_constraint_pairs(layers, design_constraints)
    incompatible_lookup = {tuple(sorted(pair)) for pair in incompatible_pairs}

    col_to_layer: List[int] = []
    layer_cols: List[List[int]] = []
    col_idx = 0
    for layer_idx, layer in enumerate(layers):
        cols: List[int] = []
        for _ in layer.get("images") or []:
            cols.append(col_idx)
            col_to_layer.append(layer_idx)
            col_idx += 1
        layer_cols.append(cols)

    if tpr < total_elements:
        return make_response(
            feasible=False,
            reason=f"Tasks per respondent must be at least {total_elements} for this layer study.",
            valid_row_variety=0,
            row_universe_rank=0,
            suggestions=[
                "Increase tasks per respondent.",
                "Reduce the number of layer images.",
            ],
        )

    valid_row_variety = 0
    basis_rows: List[np.ndarray] = []
    row_universe_rank = 0
    c = len(layers)
    min_a = min(2, c) if c >= 1 else 1
    max_a = c if c >= 1 else 1

    def is_allowed(candidate: int, selected: List[int]) -> bool:
        return all(tuple(sorted((candidate, prior))) not in incompatible_lookup for prior in selected)

    def observe_valid_row(selected: List[int]) -> bool:
        nonlocal valid_row_variety, row_universe_rank, basis_rows
        valid_row_variety += 1

        if row_universe_rank < total_elements:
            row = np.zeros(total_elements, dtype=int)
            row[selected] = 1
            trial_rows = basis_rows + [row]
            trial_rank = int(np.linalg.matrix_rank(np.vstack(trial_rows)))
            if trial_rank > row_universe_rank:
                basis_rows.append(row)
                row_universe_rank = trial_rank

        return valid_row_variety >= required_variety and row_universe_rank >= total_elements

    for k in range(min_a, max_a + 1):
        for active_layers in combinations(range(c), k):
            selected: List[int] = []

            def backtrack(depth: int) -> bool:
                if depth == len(active_layers):
                    return observe_valid_row(selected)
                layer_idx = active_layers[depth]
                for candidate in layer_cols[layer_idx]:
                    if not is_allowed(candidate, selected):
                        continue
                    selected.append(candidate)
                    enough = backtrack(depth + 1)
                    selected.pop()
                    if enough:
                        return True
                return False

            if backtrack(0):
                return make_response(
                    feasible=True,
                    reason="Design constraints are feasible.",
                    valid_row_variety=valid_row_variety,
                    row_universe_rank=row_universe_rank,
                )

    if valid_row_variety < required_variety:
        return make_response(
            feasible=False,
            reason="Not enough valid layer combinations are available.",
            valid_row_variety=valid_row_variety,
            row_universe_rank=row_universe_rank,
            suggestions=[
                "Remove or relax some design constraints.",
                "Add more images to constrained layers.",
                "Reduce tasks per respondent.",
            ],
        )

    if row_universe_rank < total_elements:
        return make_response(
            feasible=False,
            reason="Design constraints make the study unsolvable because the valid task combinations cannot identify all layer images independently.",
            valid_row_variety=valid_row_variety,
            row_universe_rank=row_universe_rank,
            suggestions=[
                "Remove or relax constraints around overlapping images.",
                "Add more images to constrained layers.",
                "Increase tasks per respondent after loosening constraints.",
            ],
        )

    return make_response(
        feasible=True,
        reason="Design constraints are feasible.",
        valid_row_variety=valid_row_variety,
        row_universe_rank=row_universe_rank,
        suggestions=[],
    )


def _tasks_from_matrix_grid(
    X: np.ndarray,
    n_gen: int,
    T: int,
    categories_data: List[Dict],
    category_info: Dict[str, List[str]],
    colmap: List[Tuple[str, int, str]],
) -> Dict[str, List[Dict[str, Any]]]:
    if not colmap:
        return {}
    tasks_structure: Dict[str, List[Dict[str, Any]]] = {}

    for respondent_id in range(n_gen):
        respondent_tasks: List[Dict[str, Any]] = []
        respondent_id_1based = respondent_id + 1
        for task_index in range(T):
            row = X[respondent_id * T + task_index]
            elements_shown: Dict[str, int] = {}
            elements_shown_content: Dict[str, Optional[Dict[str, Any]]] = {}

            for col_idx, (category_name, elem_idx, element_name) in enumerate(colmap):
                element_active = int(row[col_idx])
                elements_shown[element_name] = element_active
                if element_active:
                    for category in categories_data:
                        if category["category_name"] == category_name and elem_idx < len(
                            category.get("elements") or []
                        ):
                            element = category["elements"][elem_idx]
                            elements_shown_content[element_name] = {
                                "element_id": element["element_id"],
                                "name": element["name"],
                                "content": element["content"],
                                "alt_text": element.get("alt_text", element["name"]),
                                "element_type": element["element_type"],
                                "category_name": category_name,
                            }
                            break
                    else:
                        elements_shown_content[element_name] = None
                else:
                    elements_shown_content[element_name] = None

            elements_shown = {k: v for k, v in elements_shown.items() if not k.endswith("_ref")}
            elements_shown_content = {
                k: v for k, v in elements_shown_content.items() if not k.endswith("_ref")
            }

            respondent_tasks.append(
                {
                    "task_id": f"{respondent_id_1based}_{task_index}",
                    "elements_shown": elements_shown,
                    "elements_shown_content": elements_shown_content,
                    "task_index": task_index,
                }
            )
        tasks_structure[str(respondent_id_1based)] = respondent_tasks

    return tasks_structure


def _tasks_from_matrix_layer(
    X: np.ndarray,
    n_gen: int,
    T: int,
    layers_data: List[Dict],
    category_info: Dict[str, List[str]],
    colmap: List[Tuple[str, int, str]],
) -> Dict[str, List[Dict[str, Any]]]:
    if not colmap:
        return {}
    tasks_structure: Dict[str, List[Dict[str, Any]]] = {}

    for respondent_id in range(n_gen):
        respondent_tasks: List[Dict[str, Any]] = []
        respondent_id_1based = respondent_id + 1
        for task_index in range(T):
            row = X[respondent_id * T + task_index]
            elements_shown: Dict[str, int] = {}
            elements_shown_content: Dict[str, Optional[Dict[str, Any]]] = {}

            for col_idx, (layer_name, img_idx, element_name) in enumerate(colmap):
                element_active = int(row[col_idx])
                elements_shown[element_name] = element_active
                if element_active:
                    if "_" in element_name:
                        ln, img_index_str = element_name.rsplit("_", 1)
                        try:
                            img_i = int(img_index_str) - 1
                            for layer in layers_data:
                                if layer["name"] == ln and img_i < len(layer.get("images") or []):
                                    image = layer["images"][img_i]
                                    elements_shown_content[element_name] = {
                                        "url": image.get("url", ""),
                                        "name": image.get("name", ""),
                                        "alt_text": image.get("alt", image.get("alt_text", "")),
                                        "layer_name": ln,
                                        "z_index": layer.get("z_index", 0),
                                    }
                                    break
                            else:
                                elements_shown_content[element_name] = None
                        except ValueError:
                            elements_shown_content[element_name] = None
                    else:
                        elements_shown_content[element_name] = None
                else:
                    elements_shown_content[element_name] = None

            elements_shown = {k: v for k, v in elements_shown.items() if not k.endswith("_ref")}
            elements_shown_content = {
                k: v for k, v in elements_shown_content.items() if not k.endswith("_ref")
            }

            respondent_tasks.append(
                {
                    "task_id": f"{respondent_id_1based}_{task_index}",
                    "elements_shown": elements_shown,
                    "elements_shown_content": elements_shown_content,
                    "task_index": task_index,
                }
            )
        tasks_structure[str(respondent_id_1based)] = respondent_tasks

    return tasks_structure


def _compact_report(report: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in report.items() if k != "X"}
    pf = out.get("preflight")
    if isinstance(pf, dict):
        sv = pf.get("structural_vif")
        try:
            sv_out = float(sv) if sv is not None and np.isfinite(sv) else None
        except (TypeError, ValueError):
            sv_out = None
        out["preflight"] = {
            "feasible": pf.get("feasible"),
            "row_variety": pf.get("row_variety"),
            "structural_vif": sv_out,
            "tp_ratio": pf.get("tp_ratio"),
            "tp_class": pf.get("tp_class"),
        }
    return out


def generate_grid_tasks_golden(
    categories_data: List[Dict],
    number_of_respondents: int,
    exposure_tolerance_cv: float = 1.0,
    seed: Optional[int] = None,
    tasks_per_respondent: int = 0,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Any]:
    """
    Same return shape as generate_grid_tasks_v2: ``tasks`` + ``metadata``.
    ``tasks_per_respondent``: 0 = auto (ceil(1.5 * P) inside Golden Matrix).
    """
    tasks_per_respondent = max(0, int(tasks_per_respondent or 0))
    cats = _filter_nonempty_categories(categories_data)
    n_gen = _requested_to_generated_n(number_of_respondents)
    if n_gen < 1 or not cats:
        return {
            "tasks": {},
            "metadata": {
                "study_type": "grid_v2",
                "categories_data": categories_data,
                "category_info": _category_info_from_categories(cats) if cats else {},
                "tasks_per_consumer": 0,
                "number_of_respondents": number_of_respondents,
                "exposure_tolerance_cv": exposure_tolerance_cv,
                "capacity": 0,
                "algorithm": "golden_matrix",
            },
        }

    category_info = _category_info_from_categories(cats)
    colmap = _column_mapping_categories(cats)
    P = len(colmap)
    if P < 1:
        return {
            "tasks": {},
            "metadata": {
                "study_type": "grid_v2",
                "categories_data": categories_data,
                "category_info": category_info,
                "tasks_per_consumer": 0,
                "number_of_respondents": number_of_respondents,
                "exposure_tolerance_cv": exposure_tolerance_cv,
                "capacity": 0,
                "algorithm": "golden_matrix",
            },
        }

    config = _build_design_config_grid(n_gen, cats, tasks_per_respondent)
    T = config.tasks_per_respondent

    try:
        _df, report = generate_golden_matrix(
            config,
            progress_callback=progress_callback,
            rng_seed=seed,
        )
    except InfeasibleConfigError as e:
        raise RuntimeError(str(e)) from e
    except RDEError as e:
        raise RuntimeError(str(e)) from e

    X = report["X"]
    tasks_structure = _tasks_from_matrix_grid(X, n_gen, T, cats, category_info, colmap)
    capacity = int(n_gen * T)

    return {
        "tasks": tasks_structure,
        "metadata": {
            "study_type": "grid_v2",
            "categories_data": categories_data,
            "category_info": category_info,
            "tasks_per_consumer": T,
            "number_of_respondents": number_of_respondents,
            "exposure_tolerance_cv": exposure_tolerance_cv,
            "capacity": capacity,
            "algorithm": "golden_matrix",
            "golden_report": _compact_report(report),
        },
    }


def generate_layer_tasks_golden(
    layers_data: List[Dict],
    number_of_respondents: int,
    exposure_tolerance_pct: float = 2.0,
    seed: Optional[int] = None,
    tasks_per_respondent: int = 0,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    design_constraints: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Same return shape as generate_layer_tasks_v2."""
    tasks_per_respondent = max(0, int(tasks_per_respondent or 0))
    layers = _filter_nonempty_layers(layers_data)
    n_gen = _requested_to_generated_n(number_of_respondents)
    if n_gen < 1 or not layers:
        return {
            "tasks": {},
            "metadata": {
                "study_type": "layer_v2",
                "layers_data": layers_data,
                "category_info": _category_info_from_layers(layers) if layers else {},
                "tasks_per_consumer": 0,
                "number_of_respondents": number_of_respondents,
                "exposure_tolerance_pct": exposure_tolerance_pct,
                "capacity": 0,
                "background_image_url": None,
                "algorithm": "golden_matrix",
            },
        }

    category_info = _category_info_from_layers(layers)
    colmap = _column_mapping_layers(layers)
    P = len(colmap)
    if P < 1:
        return {
            "tasks": {},
            "metadata": {
                "study_type": "layer_v2",
                "layers_data": layers_data,
                "category_info": category_info,
                "tasks_per_consumer": 0,
                "number_of_respondents": number_of_respondents,
                "exposure_tolerance_pct": exposure_tolerance_pct,
                "capacity": 0,
                "background_image_url": None,
                "algorithm": "golden_matrix",
            },
        }

    incompatible_pairs, skipped_constraints = _layer_constraint_pairs(layers, design_constraints)
    config = _build_design_config_layer(n_gen, layers, tasks_per_respondent, incompatible_pairs)
    T = config.tasks_per_respondent

    try:
        _df, report = generate_golden_matrix(
            config,
            progress_callback=progress_callback,
            rng_seed=seed,
        )
    except InfeasibleConfigError as e:
        raise RuntimeError(str(e)) from e
    except RDEError as e:
        raise RuntimeError(str(e)) from e

    X = report["X"]
    tasks_structure = _tasks_from_matrix_layer(X, n_gen, T, layers, category_info, colmap)
    capacity = int(n_gen * T)

    return {
        "tasks": tasks_structure,
        "metadata": {
            "study_type": "layer_v2",
            "layers_data": layers_data,
            "category_info": category_info,
            "tasks_per_consumer": T,
            "number_of_respondents": number_of_respondents,
            "exposure_tolerance_pct": exposure_tolerance_pct,
            "capacity": capacity,
            "background_image_url": None,
            "algorithm": "golden_matrix",
            "design_constraints_count": len(design_constraints or []),
            "incompatible_pairs_count": len(incompatible_pairs),
            "skipped_constraint_refs": skipped_constraints,
            "golden_report": _compact_report(report),
        },
    }
